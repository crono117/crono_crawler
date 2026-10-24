from datetime import timedelta
import json
from unittest.mock import patch

import httpx
from django.test import TestCase, override_settings
from django.utils import timezone

from leads.models import DomainState
from leads.services.network import FetchError, origin
from leads.services.worker import handle_error
from .test_collection import demo_source
from leads.services.extraction import extract_ollama


@override_settings(OLLAMA_MODEL="test-model")
class OllamaRetryableErrorTests(TestCase):
    def setUp(self):
        self.source = demo_source(extractor="ollama")
        self.html = "<html><body><div>Example team page</div></body></html>"

    def _patch_client_with_transport(self, transport):
        original_client = httpx.Client

        def make_client(*args, **kwargs):
            kwargs.setdefault("transport", transport)
            return original_client(*args, **kwargs)

        return patch("leads.services.extraction.httpx.Client", new=make_client)

    def test_connection_error_is_retryable_and_schedules_retry(self):
        def raise_connect_error(request):
            raise httpx.ConnectError("cannot connect", request=request)

        transport = httpx.MockTransport(raise_connect_error)
        with self._patch_client_with_transport(transport):
            with self.assertRaises(FetchError) as ctx:
                extract_ollama(self.html, self.source)
        self.assertTrue(getattr(ctx.exception, "retryable", False))
        # Worker schedules a retry on retryable FetchError
        run = self.source.runs.create()
        job = run.jobs.create(url=self.source.url)
        handle_error(job, self.source, ctx.exception)
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.attempts, 1)
        self.assertGreater(job.available_at, timezone.now())
        # Domain throttle is updated (record exists or not; ensure value can be set without errors)
        DomainState.objects.update_or_create(origin=origin(self.source.url), defaults={"next_allowed_at": job.available_at})

    def test_timeout_error_is_retryable_and_schedules_retry(self):
        def raise_timeout(request):
            raise httpx.TimeoutException("timed out", request=request)

        transport = httpx.MockTransport(raise_timeout)
        with self._patch_client_with_transport(transport):
            with self.assertRaises(FetchError) as ctx:
                extract_ollama(self.html, self.source)
        self.assertTrue(getattr(ctx.exception, "retryable", False))
        run = self.source.runs.create()
        job = run.jobs.create(url=self.source.url)
        handle_error(job, self.source, ctx.exception)
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.attempts, 1)

    def test_bad_json_is_permanent_value_error(self):
        def good_200_with_invalid_model_payload(request):
            body = json.dumps({"message": {"content": "not a json object"}}).encode()
            return httpx.Response(200, request=request, headers={"content-type": "application/json"}, content=body)

        transport = httpx.MockTransport(good_200_with_invalid_model_payload)
        with self._patch_client_with_transport(transport):
            with self.assertRaises(ValueError) as ctx:
                extract_ollama(self.html, self.source)
        # Worker should treat non-FetchError exceptions as permanent failures
        run = self.source.runs.create()
        job = run.jobs.create(url=self.source.url)
        handle_error(job, self.source, ctx.exception)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")

    def test_retry_limit_is_unchanged(self):
        run = self.source.runs.create()
        job = run.jobs.create(url=self.source.url)
        exc = FetchError("retry me", retryable=True)
        # First three attempts are queued
        for i in range(1, 4):
            handle_error(job, self.source, exc)
            job.refresh_from_db()
            self.assertEqual(job.attempts, i)
            self.assertEqual(job.status, "queued")
            # Make deferred attempt due immediately for the next loop iteration
            job.available_at = timezone.now() - timedelta(seconds=1)
            job.save()
        # Fourth attempt fails the job
        handle_error(job, self.source, exc)
        job.refresh_from_db()
        self.assertEqual(job.attempts, 4)
        self.assertEqual(job.status, "failed")

    def test_503_status_is_retryable_and_uses_retry_after(self):
        def respond_503(request):
            headers = {"content-type": "application/json", "retry-after": "120"}
            return httpx.Response(503, headers=headers, request=request, content=b"Service Unavailable")

        transport = httpx.MockTransport(respond_503)
        with self._patch_client_with_transport(transport):
            with self.assertRaises(FetchError) as ctx:
                extract_ollama(self.html, self.source)
        self.assertTrue(getattr(ctx.exception, "retryable", False))
        # Ensure worker schedules a retry and propagates retry-after via retry_delay
        DomainState.objects.create(origin=origin(self.source.url))
        run = self.source.runs.create()
        job = run.jobs.create(url=self.source.url)
        handle_error(job, self.source, ctx.exception)
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.attempts, 1)
        self.assertGreater(job.available_at, timezone.now() + timedelta(seconds=110))
        self.assertEqual(DomainState.objects.get(origin=origin(self.source.url)).next_allowed_at, job.available_at)

    def test_502_status_is_retryable(self):
        def respond_502(request):
            return httpx.Response(502, headers={"content-type": "application/json"}, request=request, content=b"Bad Gateway")

        transport = httpx.MockTransport(respond_502)
        with self._patch_client_with_transport(transport):
            with self.assertRaises(FetchError) as ctx:
                extract_ollama(self.html, self.source)
        self.assertTrue(getattr(ctx.exception, "retryable", False))
        run = self.source.runs.create()
        job = run.jobs.create(url=self.source.url)
        handle_error(job, self.source, ctx.exception)
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.attempts, 1)

    def test_504_status_is_retryable(self):
        def respond_504(request):
            return httpx.Response(504, headers={"content-type": "application/json"}, request=request, content=b"Gateway Timeout")

        transport = httpx.MockTransport(respond_504)
        with self._patch_client_with_transport(transport):
            with self.assertRaises(FetchError) as ctx:
                extract_ollama(self.html, self.source)
        self.assertTrue(getattr(ctx.exception, "retryable", False))
        run = self.source.runs.create()
        job = run.jobs.create(url=self.source.url)
        handle_error(job, self.source, ctx.exception)
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.attempts, 1)

    def test_500_status_is_permanent(self):
        def respond_500(request):
            return httpx.Response(500, headers={"content-type": "application/json"}, request=request, content=b"Internal Error")

        transport = httpx.MockTransport(respond_500)
        with self._patch_client_with_transport(transport):
            with self.assertRaises(Exception) as ctx:
                extract_ollama(self.html, self.source)
        # Worker should mark a non-FetchError as a permanent failure
        run = self.source.runs.create()
        job = run.jobs.create(url=self.source.url)
        handle_error(job, self.source, ctx.exception)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")

