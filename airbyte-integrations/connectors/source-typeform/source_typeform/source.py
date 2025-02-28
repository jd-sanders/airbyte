#
# Copyright (c) 2021 Airbyte, Inc., all rights reserved.
#


import urllib.parse as urlparse
from abc import ABC, abstractmethod
from typing import Any, Iterable, List, Mapping, MutableMapping, Optional, Tuple
from urllib.parse import parse_qs

import pendulum
import requests
from airbyte_cdk import AirbyteLogger
from airbyte_cdk.models import SyncMode
from airbyte_cdk.sources import AbstractSource
from airbyte_cdk.sources.streams import Stream
from airbyte_cdk.sources.streams.http import HttpStream
from airbyte_cdk.sources.streams.http.auth import TokenAuthenticator
from pendulum.datetime import DateTime


class TypeformStream(HttpStream, ABC):
    url_base = "https://api.typeform.com/"
    # maximum number of entities in API response per single page
    limit: int = 200
    date_format: str = "YYYY-MM-DDTHH:mm:ss[Z]"

    def __init__(self, **kwargs: Mapping[str, Any]):
        super().__init__(authenticator=kwargs["authenticator"])
        self.config: Mapping[str, Any] = kwargs
        self.start_date: DateTime = pendulum.from_format(kwargs["start_date"], self.date_format)

        # changes page limit, this param is using for development and debugging
        if kwargs.get("page_size"):
            self.limit = kwargs.get("page_size")

    def next_page_token(self, response: requests.Response) -> Optional[Any]:
        return None

    def parse_response(self, response: requests.Response, **kwargs) -> Iterable[Mapping]:
        yield from response.json()["items"]


class TrimForms(TypeformStream):
    """
    This stream is responsible for fetching list of from_id(s) which required to process data from Forms and Responses.
    API doc: https://developer.typeform.com/create/reference/retrieve-forms/
    """

    primary_key = "id"

    def path(
        self,
        stream_state: Mapping[str, Any] = None,
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Optional[Any] = None,
    ) -> str:
        return "forms"

    def next_page_token(self, response: requests.Response) -> Optional[Any]:
        page = self.get_current_page_token(response.url)
        # stop pagination if current page equals to total pages
        return None if not page or response.json()["page_count"] <= page else page + 1

    def get_current_page_token(self, url: str) -> Optional[int]:
        """
        Fetches page query parameter from URL
        """
        parsed = urlparse.urlparse(url)
        page = parse_qs(parsed.query).get("page")
        return int(page[0]) if page else None

    def request_params(
        self,
        stream_state: Mapping[str, Any],
        stream_slice: Mapping[str, any] = None,
        next_page_token: Optional[Any] = None,
    ) -> MutableMapping[str, Any]:
        params = {"page_size": self.limit}
        params["page"] = next_page_token or 1
        return params


class TrimFormsMixin:
    def stream_slices(self, **kwargs) -> Iterable[Optional[Mapping[str, any]]]:
        form_ids = self.config.get("form_ids", [])
        if form_ids:
            for item in form_ids:
                yield {"form_id": item}
        else:
            for item in TrimForms(**self.config).read_records(sync_mode=SyncMode.full_refresh):
                yield {"form_id": item["id"]}

        yield from []


class Forms(TrimFormsMixin, TypeformStream):
    """
    This stream is responsible for detailed information about Form.
    API doc: https://developer.typeform.com/create/reference/retrieve-form/
    """

    primary_key = "id"

    def path(
        self,
        stream_state: Mapping[str, Any] = None,
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Optional[Any] = None,
    ) -> str:
        return f"forms/{stream_slice['form_id']}"

    def parse_response(self, response: requests.Response, **kwargs) -> Iterable[Mapping]:
        yield response.json()


class IncrementalTypeformStream(TypeformStream, ABC):
    cursor_field: str = "submitted_at"
    token_field: str = "token"

    @property
    def limit(self):
        return super().limit

    state_checkpoint_interval = limit

    @abstractmethod
    def get_updated_state(
        self,
        current_stream_state: MutableMapping[str, Any],
        latest_record: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        pass

    def next_page_token(self, response: requests.Response) -> Optional[Any]:
        items = response.json()["items"]
        if items and len(items) == self.limit:
            return items[-1][self.token_field]
        return None


class Responses(TrimFormsMixin, IncrementalTypeformStream):
    """
    This stream is responsible for fetching responses for particular form_id.
    API doc: https://developer.typeform.com/responses/reference/retrieve-responses/
    """

    primary_key = "response_id"
    limit: int = 1000

    def path(self, stream_slice: Optional[Mapping[str, Any]] = None, **kwargs) -> str:
        return f"forms/{stream_slice['form_id']}/responses"

    def get_form_id(self, record: Mapping[str, Any]) -> Optional[str]:
        """
        Fetches form id to which current record belongs.
        """
        referer = record.get("metadata", {}).get("referer")
        return referer.rsplit("/")[-1] if referer else None

    def get_updated_state(
        self,
        current_stream_state: MutableMapping[str, Any],
        latest_record: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        form_id = self.get_form_id(latest_record)
        if not form_id or not latest_record.get(self.cursor_field):
            return current_stream_state

        current_stream_state[form_id] = current_stream_state.get(form_id, {})
        current_stream_state[form_id][self.cursor_field] = max(
            pendulum.from_format(latest_record[self.cursor_field], self.date_format).int_timestamp,
            current_stream_state[form_id].get(self.cursor_field, 1),
        )
        return current_stream_state

    def request_params(
        self,
        stream_state: Mapping[str, Any],
        stream_slice: Mapping[str, any] = None,
        next_page_token: Optional[Any] = None,
    ) -> MutableMapping[str, Any]:
        params = {"page_size": self.limit}
        stream_state = stream_state or {}

        if not next_page_token:
            # use state for first request in incremental sync
            params["sort"] = "submitted_at,asc"
            # start from last state or from start date
            since = max(self.start_date.int_timestamp, stream_state.get(stream_slice["form_id"], {}).get(self.cursor_field, 1))
            if since:
                params["since"] = pendulum.from_timestamp(since).format(self.date_format)
        else:
            # use response token for pagination after first request
            # this approach allow to avoid data duplication within single sync
            params["after"] = next_page_token

        return params


class SourceTypeform(AbstractSource):
    def check_connection(self, logger: AirbyteLogger, config: Mapping[str, Any]) -> Tuple[bool, any]:
        try:
            form_ids = config.get("form_ids", []).copy()
            auth = TokenAuthenticator(token=config["token"])
            # verify if form inputted by user is valid
            for form in TrimForms(authenticator=auth, **config).read_records(sync_mode=SyncMode.full_refresh):
                if form.get("id") in form_ids:
                    form_ids.remove(form.get("id"))
            if form_ids:
                return False, f"Cannot find forms with IDs: {form_ids}. Please make sure they are valid form IDs and try again."
            return True, None
        except requests.exceptions.RequestException as e:
            return False, e

    def streams(self, config: Mapping[str, Any]) -> List[Stream]:
        auth = TokenAuthenticator(token=config["token"])
        return [Forms(authenticator=auth, **config), Responses(authenticator=auth, **config)]
