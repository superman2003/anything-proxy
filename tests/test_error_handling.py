import asyncio
import unittest
from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock, patch

from anything_client import AnythingClient, SUPPORTED_MODELS, get_mapped_model
from routes.admin_accounts import BatchDeleteRequest, _normalize_credit_balance, batch_delete_accounts, list_api_keys
from routes.admin_login_flow import _repair_broken_outlook_links, auto_login_all
from routes.proxy import (
    MAX_UPSTREAM_MESSAGE_CHARS,
    _extract_file_documents,
    _merge_session_documents,
    _select_hot_documents,
    derive_stable_prompt_cache_key,
    LiveSSEAdapter,
    append_style_hint,
    build_upstream_content,
    compress_tool_result_content,
    count_tokens_message,
    extract_session_key,
    fake_stream,
    get_response_style_hint,
    is_request_too_large_error,
    is_claude_code_request,
    list_models,
    normalize_usage,
    render_working_set_section,
)
from routes.admin_usage import usage_stats
from services.account_pool import AccountPool
from services.anything_login import _build_anything_cookie_header, _extract_magic_link_code, login_and_add_account
from services.outlook_client import OutlookClient


class AccountPoolErrorClassificationTests(unittest.TestCase):
    def test_quota_errors_do_not_match_permission_errors(self):
        self.assertTrue(AccountPool.is_quota_error("GraphQL error: User generation limit reached"))
        self.assertFalse(
            AccountPool.is_quota_error("GraphQL error: User is not allowed to create a new chat message")
        )
        self.assertFalse(
            AccountPool.is_quota_error("GraphQL error: User does not have access to the requested project group.")
        )

    def test_retryable_account_errors_match_permission_failures(self):
        self.assertTrue(
            AccountPool.is_retryable_account_error(
                "GraphQL error: User does not have access to the requested project group."
            )
        )
        self.assertFalse(AccountPool.is_retryable_account_error("GraphQL error: User generation limit reached"))
        self.assertFalse(
            AccountPool.is_retryable_account_error(
                "GraphQL error: User is not allowed to create a new chat message"
            )
        )

    def test_permission_blocked_errors_are_detected(self):
        self.assertTrue(
            AccountPool.is_permission_blocked_error(
                "GraphQL error: User is not allowed to create a new chat message"
            )
        )
        self.assertFalse(
            AccountPool.is_permission_blocked_error(
                "GraphQL error: User does not have access to the requested project group."
            )
        )


class AnythingClientRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_refreshes_project_group_then_raises_permission_error(self):
        client = AnythingClient()
        client._do_send = AsyncMock(
            side_effect=[
                Exception("GraphQL error: User does not have access to the requested project group."),
                Exception("GraphQL error: User is not allowed to create a new chat message"),
            ]
        )
        client._refresh_project_group = AsyncMock(return_value=True)
        client.refresh_access_token = AsyncMock(return_value=True)

        with self.assertRaisesRegex(Exception, "User is not allowed to create a new chat message"):
            await client.send_message("hello", "ANTHROPIC_CLAUDE_OPUS_4_6")

        self.assertEqual(client._do_send.await_count, 2)
        client._refresh_project_group.assert_awaited_once()
        client.refresh_access_token.assert_not_awaited()


class AnythingCookieHeaderTests(unittest.TestCase):
    def test_sonnet_46_maps_to_supported_anything_enum(self):
        self.assertEqual(get_mapped_model("claude-sonnet-4-6"), "ANTHROPIC_CLAUDE_SONNET_4")
        self.assertEqual(get_mapped_model("claude-sonnet-4-6[1m]"), "ANTHROPIC_CLAUDE_SONNET_4")

    def test_gpt_54_maps_to_chatgpt_integration(self):
        self.assertEqual(get_mapped_model("gpt-5.4"), "CHAT_GPT")

    def test_supported_models_match_primary_frontend_models(self):
        self.assertEqual(SUPPORTED_MODELS, ["claude-opus-4-6", "claude-sonnet-4-6", "gpt-5.4"])

    def test_anything_client_cookie_header_matches_web_shape(self):
        client = AnythingClient(access_token="access-token", refresh_token="refresh-token")
        self.assertEqual(
            client._cookie_header(),
            "lS_authToken=access-token; qid=refresh-token; refresh_token=refresh-token",
        )

    def test_anything_login_cookie_builder_matches_reference_shape(self):
        self.assertEqual(
            _build_anything_cookie_header(refresh_token="refresh-token", access_token="access-token"),
            "lS_authToken=access-token; qid=refresh-token; refresh_token=refresh-token",
        )

    def test_extract_magic_link_code_prefers_redirected_auth_url(self):
        self.assertEqual(
            _extract_magic_link_code(
                "https://url5218.anything.com/ls/click?foo=1",
                "https://www.anything.com/auth/magic-link?code=abc123&email=test@example.com",
            ),
            "abc123",
        )


class AccountPoolStateSyncTests(unittest.IsolatedAsyncioTestCase):
    @patch("services.account_pool.execute", new_callable=AsyncMock)
    async def test_record_success_persists_latest_client_state(self, execute_mock):
        pool = AccountPool()
        pool._clients[7] = AnythingClient(
            access_token="access-new",
            refresh_token="refresh-new",
            project_group_id="pg-new",
        )

        await pool.record_success(7)

        sql, params = execute_mock.await_args.args
        self.assertIn("access_token = ?", sql)
        self.assertIn("refresh_token = ?", sql)
        self.assertIn("project_group_id = ?", sql)
        self.assertEqual(params[0:3], ("access-new", "refresh-new", "pg-new"))
        self.assertEqual(params[-1], 7)

    @patch("services.account_pool.execute", new_callable=AsyncMock)
    async def test_record_failure_persists_latest_client_state(self, execute_mock):
        pool = AccountPool()
        pool._clients[9] = AnythingClient(
            access_token="access-new",
            refresh_token="refresh-new",
            project_group_id="pg-new",
        )

        await pool.record_failure(9, "GraphQL error: User does not have access to the requested project group.")

        sql, params = execute_mock.await_args.args
        self.assertIn("access_token = ?", sql)
        self.assertIn("refresh_token = ?", sql)
        self.assertIn("project_group_id = ?", sql)
        self.assertEqual(params[0:3], ("access-new", "refresh-new", "pg-new"))
        self.assertEqual(params[-1], 9)

    @patch("services.account_pool.execute", new_callable=AsyncMock)
    async def test_mark_permission_blocked_sets_status_and_persists_client_state(self, execute_mock):
        pool = AccountPool()
        pool._clients[11] = AnythingClient(
            access_token="access-new",
            refresh_token="refresh-new",
            project_group_id="pg-new",
        )

        await pool.mark_permission_blocked(
            11, "GraphQL error: User is not allowed to create a new chat message"
        )

        sql, params = execute_mock.await_args.args
        self.assertIn("status = 'permission_blocked'", sql)
        self.assertIn("access_token = ?", sql)
        self.assertIn("refresh_token = ?", sql)
        self.assertIn("project_group_id = ?", sql)
        self.assertEqual(params[0:3], ("access-new", "refresh-new", "pg-new"))
        self.assertEqual(params[-1], 11)


class AccountPoolLeaseTests(unittest.IsolatedAsyncioTestCase):
    @patch("services.account_pool.execute", new_callable=AsyncMock)
    @patch("services.account_pool.fetchall", new_callable=AsyncMock)
    async def test_pick_account_skips_accounts_already_in_use(self, fetchall_mock, execute_mock):
        pool = AccountPool()
        accounts = [
            {"id": 1, "access_token": "a1", "refresh_token": "r1", "project_group_id": "pg1", "proxy_url": None},
            {"id": 2, "access_token": "a2", "refresh_token": "r2", "project_group_id": "pg2", "proxy_url": None},
        ]
        fetchall_mock.side_effect = [accounts, accounts]

        first_id, _ = await pool.pick_account()
        second_id, _ = await pool.pick_account()

        self.assertEqual(first_id, 1)
        self.assertEqual(second_id, 2)
        self.assertEqual(pool._in_use, {1, 2})
        self.assertEqual(execute_mock.await_count, 2)

    @patch("services.account_pool.execute", new_callable=AsyncMock)
    @patch("services.account_pool.fetchall", new_callable=AsyncMock)
    async def test_release_account_allows_reuse(self, fetchall_mock, execute_mock):
        pool = AccountPool()
        accounts = [
            {"id": 1, "access_token": "a1", "refresh_token": "r1", "project_group_id": "pg1", "proxy_url": None},
        ]
        fetchall_mock.side_effect = [accounts, accounts]

        first_id, _ = await pool.pick_account()
        await pool.release_account(first_id)
        second_id, _ = await pool.pick_account()

        self.assertEqual(first_id, 1)
        self.assertEqual(second_id, 1)
        self.assertEqual(pool._in_use, {1})
        self.assertEqual(execute_mock.await_count, 2)


class AnythingLoginFlowTests(unittest.IsolatedAsyncioTestCase):
    @patch("services.anything_login.verify_magic_link_code", new_callable=AsyncMock)
    @patch("services.anything_login.account_pool.load", new_callable=AsyncMock)
    @patch("services.anything_login.execute", new_callable=AsyncMock)
    @patch("services.anything_login.get_tokens_from_refresh", new_callable=AsyncMock)
    @patch("services.anything_login.open_magic_link", new_callable=AsyncMock)
    @patch("services.anything_login.OutlookClient")
    @patch("services.anything_login.request_magic_link", new_callable=AsyncMock)
    async def test_same_session_verify_error_does_not_retry_with_new_client(
        self,
        request_magic_link_mock,
        outlook_client_cls,
        open_magic_link_mock,
        get_tokens_from_refresh_mock,
        execute_mock,
        account_pool_load_mock,
        verify_magic_link_code_mock,
    ):
        request_magic_link_mock.return_value = {"accessToken": None, "projectGroup": None}
        outlook_client_cls.return_value.poll_magic_link = AsyncMock(
            return_value="https://url5218.anything.com/ls/click?foo=1"
        )
        open_magic_link_mock.return_value = {
            "refresh_token": "",
            "access_token": "",
            "cookie_header": "",
            "final_url": "https://www.anything.com/auth/magic-link?code=abc123&email=user@example.com",
            "redirect_chain": ["https://www.anything.com/auth/magic-link?code=abc123&email=user@example.com"],
            "code": "abc123",
            "verify_error": "Magic Link 验证 GraphQL 错误: Invalid Magic Link Code",
        }

        result = await login_and_add_account(
            outlook_id=9,
            email="user@example.com",
            ms_refresh_token="ms-refresh",
            client_id="client-id",
        )

        self.assertFalse(result["success"])
        self.assertIn("Invalid Magic Link Code", result["error"])
        verify_magic_link_code_mock.assert_not_awaited()
        get_tokens_from_refresh_mock.assert_not_awaited()
        account_pool_load_mock.assert_not_awaited()

    @patch("services.anything_login.account_pool.load", new_callable=AsyncMock)
    @patch("services.anything_login.execute", new_callable=AsyncMock)
    @patch("services.anything_login.get_tokens_from_refresh", new_callable=AsyncMock)
    @patch("services.anything_login.open_magic_link", new_callable=AsyncMock)
    @patch("services.anything_login.OutlookClient")
    @patch("services.anything_login.request_magic_link", new_callable=AsyncMock)
    @patch("services.anything_login.AnythingClient")
    async def test_direct_signup_without_refresh_token_falls_through_magic_link_recovery(
        self,
        anything_client_cls,
        request_magic_link_mock,
        outlook_client_cls,
        open_magic_link_mock,
        get_tokens_from_refresh_mock,
        execute_mock,
        account_pool_load_mock,
    ):
        request_magic_link_mock.return_value = {
            "accessToken": "direct-access",
            "projectGroup": {"id": "pg-direct"},
            "user": {"email": "user@example.com", "username": "user"},
        }
        outlook_client_cls.return_value.poll_magic_link = AsyncMock(
            return_value="https://url5218.anything.com/ls/click?foo=1"
        )
        open_magic_link_mock.return_value = {
            "refresh_token": "cookie-refresh",
            "access_token": "cookie-access",
            "cookie_header": "lS_authToken=cookie-access; qid=cookie-refresh; refresh_token=cookie-refresh",
            "final_url": "https://www.anything.com/app",
            "redirect_chain": [
                "https://www.anything.com/auth/magic-link?code=abc123&email=user@example.com"
            ],
        }
        get_tokens_from_refresh_mock.return_value = {
            "access_token": "refreshed-access",
            "refresh_token": "refreshed-refresh",
            "cookie_header": "lS_authToken=refreshed-access; qid=refreshed-refresh; refresh_token=refreshed-refresh",
        }
        api_client = anything_client_cls.return_value
        api_client.get_me = AsyncMock(return_value={"email": "user@example.com", "username": "user"})
        api_client.get_project_groups = AsyncMock(return_value=[{"id": "pg-from-api"}])
        execute_mock.side_effect = [123, None]

        result = await login_and_add_account(
            outlook_id=9,
            email="user@example.com",
            ms_refresh_token="ms-refresh",
            client_id="client-id",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "magic_link_cookie")
        outlook_client_cls.return_value.poll_magic_link.assert_awaited_once()
        get_tokens_from_refresh_mock.assert_awaited_once()
        insert_sql, insert_params = execute_mock.await_args_list[0].args
        self.assertIn("INSERT INTO accounts", insert_sql)
        self.assertEqual(insert_params[2], "refreshed-access")
        self.assertEqual(insert_params[3], "refreshed-refresh")
        self.assertEqual(insert_params[4], "pg-from-api")
        account_pool_load_mock.assert_awaited_once()

    @patch("services.anything_login.account_pool.load", new_callable=AsyncMock)
    @patch("services.anything_login.execute", new_callable=AsyncMock)
    @patch("services.anything_login.get_tokens_from_refresh", new_callable=AsyncMock)
    @patch("services.anything_login.open_magic_link", new_callable=AsyncMock)
    @patch("services.anything_login.OutlookClient")
    @patch("services.anything_login.request_magic_link", new_callable=AsyncMock)
    @patch("services.anything_login.AnythingClient")
    async def test_login_and_add_account_can_skip_immediate_account_pool_reload(
        self,
        anything_client_cls,
        request_magic_link_mock,
        outlook_client_cls,
        open_magic_link_mock,
        get_tokens_from_refresh_mock,
        execute_mock,
        account_pool_load_mock,
    ):
        request_magic_link_mock.return_value = {
            "accessToken": "direct-access",
            "projectGroup": {"id": "pg-direct"},
            "user": {"email": "user@example.com", "username": "user"},
        }
        outlook_client_cls.return_value.poll_magic_link = AsyncMock(
            return_value="https://url5218.anything.com/ls/click?foo=1"
        )
        open_magic_link_mock.return_value = {
            "refresh_token": "cookie-refresh",
            "access_token": "cookie-access",
            "cookie_header": "lS_authToken=cookie-access; qid=cookie-refresh; refresh_token=cookie-refresh",
            "final_url": "https://www.anything.com/app",
            "redirect_chain": [
                "https://www.anything.com/auth/magic-link?code=abc123&email=user@example.com"
            ],
        }
        get_tokens_from_refresh_mock.return_value = {
            "access_token": "refreshed-access",
            "refresh_token": "refreshed-refresh",
            "cookie_header": "lS_authToken=refreshed-access; qid=refreshed-refresh; refresh_token=refreshed-refresh",
        }
        api_client = anything_client_cls.return_value
        api_client.get_me = AsyncMock(return_value={"email": "user@example.com", "username": "user"})
        api_client.get_project_groups = AsyncMock(return_value=[{"id": "pg-from-api"}])
        execute_mock.side_effect = [123, None]

        result = await login_and_add_account(
            outlook_id=9,
            email="user@example.com",
            ms_refresh_token="ms-refresh",
            client_id="client-id",
            reload_account_pool=False,
        )

        self.assertTrue(result["success"])
        account_pool_load_mock.assert_not_awaited()


class BatchDeleteAccountsTests(unittest.IsolatedAsyncioTestCase):
    @patch("routes.admin_accounts.account_pool.load", new_callable=AsyncMock)
    @patch("routes.admin_accounts.account_pool.invalidate")
    @patch("routes.admin_accounts.execute", new_callable=AsyncMock)
    @patch("routes.admin_accounts.fetchall", new_callable=AsyncMock)
    async def test_batch_delete_accounts_deletes_existing_ids_and_reports_missing(
        self,
        fetchall_mock,
        execute_mock,
        invalidate_mock,
        account_pool_load_mock,
    ):
        fetchall_mock.return_value = [{"id": 2}, {"id": 5}]

        result = await batch_delete_accounts(BatchDeleteRequest(ids=[5, 2, 5, 99]))

        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["ids"], [2, 5])
        self.assertEqual(result["missing_ids"], [99])
        sql, params = execute_mock.await_args.args
        self.assertIn("DELETE FROM accounts WHERE id IN (?, ?)", sql)
        self.assertEqual(params, (2, 5))
        self.assertEqual(invalidate_mock.call_count, 2)
        invalidate_mock.assert_any_call(2)
        invalidate_mock.assert_any_call(5)
        account_pool_load_mock.assert_awaited_once()


class UsageStatsTests(unittest.IsolatedAsyncioTestCase):
    @patch("routes.admin_usage.fetchall", new_callable=AsyncMock)
    @patch("routes.admin_usage.fetchone", new_callable=AsyncMock)
    async def test_usage_stats_returns_status_breakdown_without_account_list(
        self,
        fetchone_mock,
        fetchall_mock,
    ):
        fetchone_mock.return_value = {
            "total_requests": 21,
            "total_input_tokens": 100,
            "total_output_tokens": 200,
            "total_cache_read": 30,
            "total_cache_write": 40,
            "total_tokens": 370,
            "avg_duration_ms": 5455,
            "error_count": 5,
        }
        fetchall_mock.side_effect = [
            [{
                "model": "claude-opus-4-6",
                "requests": 10,
                "input_tokens": 100,
                "output_tokens": 200,
                "cache_read": 30,
                "cache_write": 40,
                "total_tokens": 1000,
            }],
            [{"status": "error", "requests": 5, "total_tokens": 200}],
            [{
                "date": "2026-04-06",
                "requests": 21,
                "input_tokens": 100,
                "output_tokens": 200,
                "cache_read_tokens": 30,
                "cache_write_tokens": 40,
                "total_tokens": 370,
            }],
            [{
                "api_key_id": "sk-anything-test",
                "key_id": 1,
                "key_name": "test",
                "model": "claude-opus-4-6",
                "requests": 2,
                "input_tokens": 100,
                "output_tokens": 200,
                "cache_read_tokens": 30,
                "cache_write_tokens": 40,
                "total_tokens": 370,
            }],
        ]

        result = await usage_stats(days=7)

        self.assertIn("by_status", result)
        self.assertNotIn("by_account", result)
        self.assertIn("pricing_catalog", result)
        self.assertIn("total_cost_usd", result["totals"])
        self.assertEqual(result["by_model"][0]["model"], "claude-opus-4-6")
        self.assertIn("pricing", result["by_model"][0])
        self.assertEqual(result["by_status"][0]["status"], "error")
        self.assertEqual(result["by_status"][0]["requests"], 5)


class OutlookClientTests(unittest.TestCase):
    @patch.object(OutlookClient, "_delete_email")
    @patch.object(OutlookClient, "_fetch_and_extract")
    @patch.object(OutlookClient, "_search_magic_link_emails")
    @patch.object(OutlookClient, "_connect_imap")
    def test_poll_once_keeps_email_in_same_second_as_since(
        self,
        connect_imap_mock,
        search_mock,
        fetch_mock,
        delete_mock,
    ):
        imap = connect_imap_mock.return_value
        search_mock.return_value = ["12"]
        fetch_mock.return_value = {
            "link": "https://same-second.example.com",
            "received_at": datetime(2026, 4, 6, 8, 52, 40, tzinfo=timezone.utc),
        }
        client = OutlookClient("ms-refresh", "client-id", email_address="user@example.com")

        link = client._poll_once(datetime(2026, 4, 6, 8, 52, 40, 167881, tzinfo=timezone.utc))

        self.assertEqual(link, "https://same-second.example.com")
        delete_mock.assert_called_once_with(imap, "12")

    @patch.object(OutlookClient, "_delete_email")
    @patch.object(OutlookClient, "_fetch_and_extract")
    @patch.object(OutlookClient, "_search_magic_link_emails")
    @patch.object(OutlookClient, "_connect_imap")
    def test_poll_once_uses_latest_stale_magic_link_when_allowed(
        self,
        connect_imap_mock,
        search_mock,
        fetch_mock,
        delete_mock,
    ):
        imap = connect_imap_mock.return_value
        search_mock.return_value = ["12", "11"]
        fetch_mock.side_effect = [
            {
                "link": "https://recent-stale.example.com",
                "received_at": datetime(2026, 4, 6, 8, 52, 35, tzinfo=timezone.utc),
            },
            {
                "link": "https://older-stale.example.com",
                "received_at": datetime(2026, 4, 6, 8, 52, 10, tzinfo=timezone.utc),
            },
        ]
        client = OutlookClient("ms-refresh", "client-id", email_address="user@example.com")

        link = client._poll_once(datetime(2026, 4, 6, 8, 52, 40, tzinfo=timezone.utc), allow_stale=True)

        self.assertEqual(link, "https://recent-stale.example.com")
        delete_mock.assert_called_once_with(imap, "12")

    @patch.object(OutlookClient, "_delete_email")
    @patch.object(OutlookClient, "_fetch_and_extract")
    @patch.object(OutlookClient, "_search_magic_link_emails")
    @patch.object(OutlookClient, "_connect_imap")
    def test_poll_once_skips_old_magic_link_email(
        self,
        connect_imap_mock,
        search_mock,
        fetch_mock,
        delete_mock,
    ):
        imap = connect_imap_mock.return_value
        search_mock.return_value = ["10", "9"]
        fetch_mock.side_effect = [
            {
                "link": "https://old.example.com",
                "received_at": datetime(2026, 4, 6, 10, 0, tzinfo=timezone.utc),
            },
            {
                "link": "https://new.example.com",
                "received_at": datetime(2026, 4, 6, 12, 1, tzinfo=timezone.utc),
            },
        ]
        client = OutlookClient("ms-refresh", "client-id", email_address="user@example.com")

        link = client._poll_once(datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc))

        self.assertEqual(link, "https://new.example.com")
        delete_mock.assert_called_once_with(imap, "9")


class OutlookClientAsyncTests(unittest.IsolatedAsyncioTestCase):
    @patch("services.outlook_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("services.outlook_client.asyncio.to_thread", new_callable=AsyncMock)
    @patch.object(OutlookClient, "_ensure_token", new_callable=AsyncMock)
    async def test_poll_magic_link_enables_stale_fallback_after_30_seconds(
        self,
        ensure_token_mock,
        to_thread_mock,
        sleep_mock,
    ):
        calls = []

        async def fake_to_thread(func, since_dt, allow_stale):
            calls.append(allow_stale)
            return None

        to_thread_mock.side_effect = fake_to_thread
        client = OutlookClient("ms-refresh", "client-id", email_address="user@example.com")

        with self.assertRaises(TimeoutError):
            await client.poll_magic_link(max_attempts=6, interval=5, since="2026-04-06T08:52:40.167881+00:00")

        self.assertEqual(calls, [False, False, False, False, False, True])
        ensure_token_mock.assert_awaited_once()


class OutlookLinkRepairTests(unittest.IsolatedAsyncioTestCase):
    @patch("routes.admin_login_flow.execute", new_callable=AsyncMock)
    @patch("routes.admin_login_flow.fetchall", new_callable=AsyncMock)
    async def test_repair_broken_outlook_links_resets_stale_linked_rows(
        self,
        fetchall_mock,
        execute_mock,
    ):
        fetchall_mock.return_value = [{"id": 3}, {"id": 4}, {"id": 8}]

        repaired = await _repair_broken_outlook_links()

        self.assertEqual(repaired, 3)
        sql, params = execute_mock.await_args.args
        self.assertIn("UPDATE outlook_accounts SET status = 'pending'", sql)
        self.assertIn("WHERE id IN (?, ?, ?)", sql)
        self.assertEqual(params[1:], (3, 4, 8))

    @patch("routes.admin_login_flow.account_pool.load", new_callable=AsyncMock)
    @patch("routes.admin_login_flow.login_and_add_account", new_callable=AsyncMock)
    @patch("routes.admin_login_flow.fetchall", new_callable=AsyncMock)
    @patch("routes.admin_login_flow._repair_broken_outlook_links", new_callable=AsyncMock)
    async def test_auto_login_all_runs_with_max_concurrency_three(
        self,
        repair_mock,
        fetchall_mock,
        login_and_add_account_mock,
        account_pool_load_mock,
    ):
        fetchall_mock.return_value = [
            {"id": i, "email": f"user{i}@example.com", "ms_refresh_token": f"rt{i}", "client_id": f"cid{i}"}
            for i in range(1, 8)
        ]

        current = 0
        peak = 0

        async def fake_login(**kwargs):
            nonlocal current, peak
            self.assertFalse(kwargs["reload_account_pool"])
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            current -= 1
            return {"success": True}

        login_and_add_account_mock.side_effect = fake_login

        result = await auto_login_all()

        self.assertEqual(result["total"], 7)
        self.assertEqual(result["success"], 7)
        self.assertEqual(result["failed"], 0)
        self.assertLessEqual(peak, 3)
        account_pool_load_mock.assert_awaited_once()


class DummyRequest:
    def __init__(self, body, headers=None, query_params=None):
        self._body = body
        self.headers = headers or {}
        self.query_params = query_params or {}

    async def json(self):
        return self._body


class ProxyCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_extract_session_key_prefers_claude_code_header(self):
        request = DummyRequest({}, headers={"x-claude-code-session-id": "session-123"})
        self.assertEqual(extract_session_key(request, {"conversation_id": "conv-1"}), "session-123")

    def test_claude_code_style_hint_is_added_for_beta_requests(self):
        request = DummyRequest({}, query_params={"beta": "true"})
        self.assertTrue(is_claude_code_request(request))
        hint = get_response_style_hint(request)
        self.assertIn("自然、直接、简短地回答", hint)
        self.assertIn("不要动不动就列清单", hint)
        self.assertIn("先用一句高层概括回答", hint)
        self.assertIn("实用的编程助手", hint)

    def test_stable_prompt_cache_key_stays_same_for_later_turns_in_same_session(self):
        base_messages = [{"role": "user", "content": "帮我优化这个项目"}]
        extended_messages = base_messages + [{"role": "assistant", "content": "当然"}, {"role": "user", "content": "继续"}]

        key1 = derive_stable_prompt_cache_key(
            "claude-opus-4-6",
            base_messages,
            system="system",
            tools=[{"name": "Read", "input_schema": {"type": "object"}}],
            style_hint="自然回答",
            session_key="session-1",
        )
        key2 = derive_stable_prompt_cache_key(
            "claude-opus-4-6",
            extended_messages,
            system="system",
            tools=[{"name": "Read", "input_schema": {"type": "object"}}],
            style_hint="自然回答",
            session_key="session-1",
        )

        self.assertEqual(key1, key2)

    def test_stable_prompt_cache_key_differs_across_sessions(self):
        messages = [{"role": "user", "content": "帮我优化这个项目"}]
        key1 = derive_stable_prompt_cache_key("claude-opus-4-6", messages, session_key="session-1")
        key2 = derive_stable_prompt_cache_key("claude-opus-4-6", messages, session_key="session-2")
        self.assertNotEqual(key1, key2)

    def test_append_style_hint_adds_proxy_style_block(self):
        content = append_style_hint("[user]\nhello\n", "Keep it natural.")
        self.assertIn("[System Style Override]", content)
        self.assertIn("Keep it natural.", content)
        self.assertTrue(content.startswith("[System Style Override]"))

    def test_is_request_too_large_error_detects_upstream_limit(self):
        self.assertTrue(
            is_request_too_large_error(
                "GraphQL error: Chat message exceeds the maximum length of 200000 characters."
            )
        )
        self.assertFalse(is_request_too_large_error("GraphQL error: User generation limit reached"))

    def test_normalize_usage_fills_missing_fields(self):
        usage = normalize_usage({"input_tokens": 12, "output_tokens": None})
        self.assertEqual(
            usage,
            {
                "input_tokens": 12,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        )

    async def test_fake_stream_message_start_includes_output_tokens(self):
        chunks = []
        async for chunk in fake_stream("claude-opus-4-6", None, "hello", {"input_tokens": 10}):
            chunks.append(chunk)
            if "event: message_start" in chunk:
                break

        payload = json.loads(chunks[0].split("data: ", 1)[1])
        self.assertEqual(payload["message"]["usage"]["input_tokens"], 10)
        self.assertIn("output_tokens", payload["message"]["usage"])
        self.assertEqual(payload["message"]["usage"]["output_tokens"], 0)

    async def test_fake_stream_text_deltas_are_character_sized(self):
        chunks = []
        async for chunk in fake_stream("claude-opus-4-6", None, "你好ab", {"input_tokens": 10}):
            chunks.append(chunk)

        deltas = []
        for chunk in chunks:
            if "event: content_block_delta" not in chunk:
                continue
            payload = json.loads(chunk.split("data: ", 1)[1])
            if payload["delta"]["type"] == "text_delta":
                deltas.append(payload["delta"]["text"])

        self.assertEqual(deltas, ["你", "好", "a", "b"])

    def test_live_sse_adapter_streams_text_incrementally(self):
        adapter = LiveSSEAdapter("claude-opus-4-6", {"input_tokens": 10})
        events = [adapter.message_start()]
        events.extend(adapter.feed_text("hel"))
        events.extend(adapter.feed_text("lo world"))
        events.extend(adapter.finish({"input_tokens": 10, "output_tokens": 5}))

        deltas = []
        for event in events:
            if "event: content_block_delta" not in event:
                continue
            payload = json.loads(event.split("data: ", 1)[1])
            if payload["delta"]["type"] == "text_delta":
                deltas.append(payload["delta"]["text"])

        self.assertEqual("".join(deltas), "hello world")

    def test_live_sse_adapter_emits_tool_use_block(self):
        adapter = LiveSSEAdapter("claude-opus-4-6", {"input_tokens": 10})
        events = [adapter.message_start()]
        events.extend(adapter.feed_text("before "))
        events.extend(adapter.feed_text("```tool_use\n"))
        events.extend(adapter.feed_text('{"id":"toolu_1","name":"web_search","input":{"q":"hi"}}'))
        events.extend(adapter.feed_text("\n``` after"))
        events.extend(adapter.finish({"input_tokens": 10, "output_tokens": 5}))

        tool_starts = []
        tool_deltas = []
        text_deltas = []
        for event in events:
            if "data: " not in event:
                continue
            payload = json.loads(event.split("data: ", 1)[1])
            if event.startswith("event: content_block_start") and payload.get("content_block", {}).get("type") == "tool_use":
                tool_starts.append(payload["content_block"]["name"])
            if event.startswith("event: content_block_delta"):
                delta = payload["delta"]
                if delta["type"] == "input_json_delta":
                    tool_deltas.append(delta["partial_json"])
                if delta["type"] == "text_delta":
                    text_deltas.append(delta["text"])

        self.assertEqual(tool_starts, ["web_search"])
        self.assertEqual(text_deltas[:7], list("before "))
        self.assertTrue(any('"q": "hi"' in chunk or '"q":"hi"' in chunk for chunk in tool_deltas))

    @patch("routes.proxy._validate_api_key", new_callable=AsyncMock, return_value=True)
    async def test_count_tokens_endpoint_returns_input_tokens(self, validate_mock):
        request = DummyRequest({
            "model": "claude-opus-4-6",
            "messages": [{"role": "user", "content": "hello world"}],
        })

        response = await count_tokens_message(request, authorization="Bearer test-key")
        body = json.loads(response.body.decode("utf-8"))

        self.assertIn("input_tokens", body)
        self.assertGreater(body["input_tokens"], 0)
        validate_mock.assert_awaited_once()

    async def test_list_models_returns_anthropic_shape(self):
        response = await list_models()
        self.assertIn("data", response)
        self.assertFalse(response["has_more"])
        self.assertTrue(response["data"])
        self.assertEqual(response["data"][0]["type"], "model")
        self.assertEqual(response["data"][0]["id"], "claude-opus-4-6")
        self.assertEqual(response["data"][-1]["id"], "gpt-5.4")

    def test_build_upstream_content_compacts_oversized_context_and_keeps_latest_user_message(self):
        big_tool_result = "x" * 210000
        content, compacted = build_upstream_content(
            messages=[
                {"role": "assistant", "content": [{"type": "text", "text": "previous"}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": big_tool_result}]},
                {"role": "user", "content": "最后的问题要保留"},
            ],
            system="system prompt",
            tools=[{"name": "read_file", "description": "Read a file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}],
        )

        self.assertLessEqual(len(content), MAX_UPSTREAM_MESSAGE_CHARS)
        self.assertLessEqual(len(content), MAX_UPSTREAM_MESSAGE_CHARS)
        self.assertIn("最后的问题要保留", content)

    def test_build_upstream_content_includes_style_hint_for_claude_code(self):
        content, compacted = build_upstream_content(
            messages=[{"role": "user", "content": "你好"}],
            style_hint="Respond naturally and directly.",
        )

        self.assertFalse(compacted)
        self.assertIn("[System Style Override]", content)
        self.assertIn("Respond naturally and directly.", content)

    def test_compress_tool_result_content_keeps_file_signals(self):
        raw = (
            "Read /workspace/src/app.py\n"
            "Read C:/repo/tests/test_app.py\n"
            + ("body\n" * 1000)
        )
        summary = compress_tool_result_content(raw, 200)

        self.assertIn("tool_result summary", summary)
        self.assertIn("/workspace/src/app.py", summary)
        self.assertIn("C:/repo/tests/test_app.py", summary)

    def test_extract_file_documents_and_working_set_prioritize_recent_files(self):
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "src/a.py"}},
                    {"type": "tool_use", "id": "toolu_2", "name": "Read", "input": {"file_path": "src/b.py"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "A" * 200},
                    {"type": "tool_result", "tool_use_id": "toolu_2", "content": "B" * 200},
                ],
            },
            {"role": "user", "content": "继续修改 src/a.py"},
        ]

        current_docs = _extract_file_documents(messages)
        merged_docs = _merge_session_documents([], current_docs)
        hot_docs = _select_hot_documents(merged_docs, "继续修改 src/a.py", {"src/a.py", "src/b.py"})
        section = render_working_set_section(hot_docs)

        self.assertEqual(current_docs[0]["path"], "src/b.py")
        self.assertEqual(current_docs[1]["path"], "src/a.py")
        self.assertIn("src/a.py", section)
        self.assertIn("src/b.py", section)

    def test_build_upstream_content_uses_cached_file_reference_in_messages(self):
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "src/a.py"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "print('hello')\n" * 200},
                ],
            },
        ]

        content, compacted = build_upstream_content(
            messages=messages,
            cached_file_paths={"src/a.py"},
            session_context_section="[Session Working Set]\n### src/a.py\nprint('hello')\n",
        )

        self.assertFalse(compacted)
        self.assertIn("[cached file context for src/a.py already available in Session Working Set]", content)


class ApiKeysUsageTests(unittest.IsolatedAsyncioTestCase):
    @patch("routes.admin_accounts.fetchall", new_callable=AsyncMock)
    async def test_list_api_keys_includes_usage_and_cost(self, fetchall_mock):
        fetchall_mock.side_effect = [
            [
                {
                    "id": 1,
                    "key": "sk-anything-test",
                    "name": "Main key",
                    "is_active": 1,
                    "created_at": "2026-04-06T00:00:00+00:00",
                    "last_used_at": None,
                }
            ],
            [
                {
                    "api_key_id": "sk-anything-test",
                    "model": "claude-opus-4-6",
                    "requests": 3,
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "cache_read_tokens": 30,
                    "cache_write_tokens": 40,
                    "total_tokens": 370,
                }
            ],
        ]

        result = await list_api_keys()

        self.assertEqual(result["keys"][0]["total_requests"], 3)
        self.assertEqual(result["keys"][0]["total_tokens"], 370)
        self.assertGreater(result["keys"][0]["total_cost_usd"], 0)


class BillingNormalizationTests(unittest.TestCase):
    def test_normalize_credit_balance_accepts_numeric_string(self):
        self.assertEqual(_normalize_credit_balance("217415122000"), 217415122000.0)

    def test_normalize_credit_balance_returns_none_for_invalid_value(self):
        self.assertIsNone(_normalize_credit_balance("not-a-number"))


if __name__ == "__main__":
    unittest.main()
