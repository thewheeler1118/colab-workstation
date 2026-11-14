"""TASK-008 (Issue #57): Worker-side ComfyUI local API debounce.

A single slow /history read must never kill a healthy inference:
transient failures are retried with backoff while ComfyUI is alive,
and a /prompt submit that times out on read is resolved by checking
/queue + /history for the fixed client_id before any resubmit.
No GPU, no CU — get_json / post_json / time.sleep are all mocked.
"""
import unittest
import urllib.error
from unittest import mock

import h3_remote_worker as w


def _success_history(pid, outputs=None):
    return {pid: {"status": {"status_str": "success"},
                  "outputs": outputs or {"1": {"videos": ["a.mp4"]}}}}


class _Proc:
    """Fake ComfyUI subprocess handle: poll() None = alive, int = exited."""
    def __init__(self, rc=None):
        self._rc = rc

    def poll(self):
        return self._rc


class ExecutePromptDebounceTest(unittest.TestCase):
    def setUp(self):
        self.get_json = mock.patch.object(w, "get_json").start()
        self.post_json = mock.patch.object(w, "post_json").start()
        self.sleep = mock.patch.object(w.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)
        self.post_json.return_value = {"prompt_id": "pid-1"}

    def _run(self, proc=None, timeout_sec=600):
        return w.execute_prompt("http://127.0.0.1:8188", {"1": {}}, timeout_sec,
                                proc=proc)

    def test_transient_history_timeouts_recover(self):
        pid = "pid-1"
        self.get_json.side_effect = [
            urllib.error.URLError(TimeoutError("timed out")),
            urllib.error.URLError(TimeoutError("timed out")),
            ConnectionError("reset"),
            _success_history(pid),
        ]
        out = self._run(proc=_Proc())
        self.assertEqual(out, {"1": {"videos": ["a.mp4"]}})
        # The poll used the 30 s single-request timeout, not 10 s.
        for call in self.get_json.call_args_list:
            self.assertEqual(call.kwargs.get("timeout"),
                             w.HISTORY_POLL_TIMEOUT_SEC)
            self.assertEqual(w.HISTORY_POLL_TIMEOUT_SEC, 30)

    def test_dead_comfyui_fails_fast(self):
        self.get_json.side_effect = urllib.error.URLError(
            TimeoutError("timed out"))
        with self.assertRaises(RuntimeError) as ctx:
            self._run(proc=_Proc(rc=1))
        self.assertIn("ComfyUI process exited", str(ctx.exception))
        # One transient failure, then the fast failure — not 8 retries.
        self.assertEqual(self.get_json.call_count, 1)

    def test_nine_consecutive_transients_raise(self):
        self.get_json.side_effect = urllib.error.URLError(
            TimeoutError("timed out"))
        with self.assertRaises(RuntimeError) as ctx:
            self._run(proc=_Proc())
        self.assertIn("9 consecutive times", str(ctx.exception))
        self.assertEqual(self.get_json.call_count, 9)

    def test_non_transient_http_error_raises_immediately(self):
        self.get_json.side_effect = urllib.error.HTTPError(
            "http://x/history/pid-1", 400, "Bad Request", {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            self._run(proc=_Proc())
        self.assertEqual(self.get_json.call_count, 1)

    def test_http_502_is_transient(self):
        self.get_json.side_effect = [
            urllib.error.HTTPError("http://x/history/pid-1", 502,
                                   "Bad Gateway", {}, None),
            _success_history("pid-1"),
        ]
        out = self._run(proc=_Proc())
        self.assertEqual(out, {"1": {"videos": ["a.mp4"]}})

    def test_comfyui_error_status_still_raises(self):
        self.get_json.return_value = {
            "pid-1": {"status": {"status_str": "error"},
                      "outputs": {}}}
        with self.assertRaises(RuntimeError) as ctx:
            self._run(proc=_Proc())
        self.assertIn("ComfyUI inference error", str(ctx.exception))


class SubmitPromptIdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.post_json = mock.patch.object(w, "post_json").start()
        self.get_json = mock.patch.object(w, "get_json").start()
        self.sleep = mock.patch.object(w.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def _submit(self):
        return w._submit_prompt("http://127.0.0.1:8188", {"1": {}},
                                "muse-h3-test-client")

    def _queued(self, pid, client_id="muse-h3-test-client"):
        return {"queue_running": [[0, pid, {}, {"client_id": client_id}, []]],
                "queue_pending": []}

    def test_timeout_with_queued_prompt_recovers_without_resubmit(self):
        self.post_json.side_effect = urllib.error.URLError(
            TimeoutError("timed out"))
        self.get_json.side_effect = [self._queued("pid-9"), {}]
        result = self._submit()
        self.assertEqual(result, {"prompt_id": "pid-9"})
        self.assertEqual(self.post_json.call_count, 1)

    def test_timeout_with_history_prompt_recovers(self):
        self.post_json.side_effect = urllib.error.URLError(
            TimeoutError("timed out"))
        history = {"pid-7": {"prompt": [1, "pid-7", {}, {"client_id": "muse-h3-test-client"}, []],
                             "status": {"status_str": "success"}, "outputs": {}}}
        self.get_json.side_effect = [{"queue_running": [], "queue_pending": []},
                                     history]
        result = self._submit()
        self.assertEqual(result, {"prompt_id": "pid-7"})
        self.assertEqual(self.post_json.call_count, 1)

    def test_timeout_with_nothing_queued_resubmits_once(self):
        self.post_json.side_effect = [
            urllib.error.URLError(TimeoutError("timed out")),
            {"prompt_id": "pid-2"},
        ]
        # First probe finds nothing (empty queue + empty history);
        # the resubmit then succeeds.
        self.get_json.side_effect = [
            {"queue_running": [], "queue_pending": []}, {},
        ]
        result = self._submit()
        self.assertEqual(result, {"prompt_id": "pid-2"})
        self.assertEqual(self.post_json.call_count, 2)

    def test_double_timeout_with_nothing_queued_fails_closed(self):
        self.post_json.side_effect = urllib.error.URLError(
            TimeoutError("timed out"))
        self.get_json.return_value = {"queue_running": [], "queue_pending": []}
        with self.assertRaises(RuntimeError) as ctx:
            self._submit()
        self.assertIn("muse-h3-test-client", str(ctx.exception))
        self.assertIn("refusing a third", str(ctx.exception))
        self.assertEqual(self.post_json.call_count, 2)

    def test_node_errors_raise_without_probe(self):
        self.post_json.return_value = {"node_errors": {"1": ["bad"]}}
        with self.assertRaises(RuntimeError) as ctx:
            self._submit()
        self.assertIn("ComfyUI rejected workflow", str(ctx.exception))
        self.assertEqual(self.get_json.call_count, 0)

    def test_other_client_id_is_not_ours(self):
        # A queued prompt under a DIFFERENT client_id must not be
        # adopted; the submit is retried once instead.
        self.post_json.side_effect = [
            urllib.error.URLError(TimeoutError("timed out")),
            {"prompt_id": "pid-2"},
        ]
        self.get_json.side_effect = [
            self._queued("pid-other", client_id="someone-else"), {},
        ]
        result = self._submit()
        self.assertEqual(result, {"prompt_id": "pid-2"})


class TransientClassifierTest(unittest.TestCase):
    def test_classifier(self):
        self.assertTrue(w._is_transient_api_error(TimeoutError("t")))
        self.assertTrue(w._is_transient_api_error(
            urllib.error.URLError("conn reset")))
        self.assertTrue(w._is_transient_api_error(ConnectionError("x")))
        self.assertTrue(w._is_transient_api_error(urllib.error.HTTPError(
            "http://x", 503, "Unavailable", {}, None)))
        self.assertFalse(w._is_transient_api_error(urllib.error.HTTPError(
            "http://x", 400, "Bad Request", {}, None)))
        self.assertFalse(w._is_transient_api_error(
            RuntimeError("POST http://x failed HTTP 400: bad")))
        self.assertTrue(w._is_transient_api_error(
            RuntimeError("POST http://x failed HTTP 502: bad gateway")))
        self.assertFalse(w._is_transient_api_error(ValueError("bad")))


if __name__ == "__main__":
    unittest.main()
