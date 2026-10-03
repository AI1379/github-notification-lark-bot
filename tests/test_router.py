"""路由：按群订阅仓库的匹配与过滤。"""

from __future__ import annotations

from larkbot.fixtures import issue_comment_payload, pull_request_payload, push_payload
from larkbot.github.normalize import normalize_webhook
from larkbot.router import Router, normalize_event_names
from tests.conftest import make_config


def _push(repo: str = "acme/api", branch: str = "main", actor: str = "octocat", **kwargs):
    event = normalize_webhook("push", push_payload(repo=repo, branch=branch, actor=actor, **kwargs))
    assert event is not None
    return event


def test_wildcard_subscription_matches_all_repos_in_org(config):
    router = Router(config)
    assert [chat.name for chat in router.resolve(_push("acme/api"))] == ["dev"]
    assert [chat.name for chat in router.resolve(_push("acme/web"))] == ["dev"]
    assert router.resolve(_push("other/api")) == []


def test_defaults_events_filter_out_workflow_run(config):
    from larkbot.fixtures import workflow_run_payload

    router = Router(config)
    event = normalize_webhook("workflow_run", workflow_run_payload(repo="acme/api"))
    assert event is not None
    assert router.resolve(event) == []
    reasons = {rule.reason for rule in router.explain(event) if not rule.matched}
    assert reasons == {"event"}


def test_chat_receives_only_matching_events(config):
    """acme/api 的 release 同时进 dev（acme/*）和 rel（events=release 的专订阅）。"""
    from larkbot.fixtures import release_payload

    router = Router(config)
    event = normalize_webhook("release", release_payload(repo="acme/api"))
    assert event is not None
    assert [chat.name for chat in router.resolve(event)] == ["dev", "rel"]
    # push 不进 rel
    assert [chat.name for chat in router.resolve(_push("acme/api"))] == ["dev"]


def test_ignore_bot_actors_from_defaults(config):
    router = Router(config)
    assert router.resolve(_push("acme/api", actor="dependabot[bot]")) == []
    assert router.resolve(_push("acme/api", actor="octocat")) != []


def test_subscription_can_override_ignore_actors(config):
    config = make_config(
        subscriptions=[
            {"repos": ["acme/*"], "chats": ["dev"], "ignore_actors": []},
        ]
    )
    router = Router(config)
    assert [chat.name for chat in router.resolve(_push("acme/api", actor="github-actions[bot]"))] == ["dev"]


def test_ignore_drafts(config):
    router = Router(config)
    draft = normalize_webhook("pull_request", pull_request_payload(repo="acme/api", draft=True))
    normal = normalize_webhook("pull_request", pull_request_payload(repo="acme/api", draft=False))
    assert draft is not None and normal is not None
    assert router.resolve(draft) == []
    assert [chat.name for chat in router.resolve(normal)] == ["dev"]


def test_branch_filter(config):
    config = make_config(subscriptions=[{"repos": ["acme/api"], "chats": ["dev"], "branches": ["main", "release/*"]}])
    router = Router(config)
    assert router.resolve(_push("acme/api", branch="main")) != []
    assert router.resolve(_push("acme/api", branch="release/1.0")) != []
    assert router.resolve(_push("acme/api", branch="feature/x")) == []


def test_branch_filter_applies_to_pr_base_branch(config):
    config = make_config(subscriptions=[{"repos": ["acme/api"], "chats": ["dev"], "branches": ["main"]}])
    router = Router(config)
    on_main = normalize_webhook("pull_request", pull_request_payload(repo="acme/api", branch="main"))
    on_dev = normalize_webhook("pull_request", pull_request_payload(repo="acme/api", branch="develop"))
    assert on_main is not None and on_dev is not None
    assert router.resolve(on_main) != []
    assert router.resolve(on_dev) == []


def test_branch_filter_ignores_events_without_ref(config):
    """issue 评论没有分支概念，不应被 branches 过滤掉。"""
    config = make_config(
        defaults={"events": ["issue_comment"], "ignore_actors": []},
        subscriptions=[{"repos": ["acme/api"], "chats": ["dev"], "branches": ["main"]}],
    )
    router = Router(config)
    event = normalize_webhook("issue_comment", issue_comment_payload(repo="acme/api"))
    assert event is not None
    assert [chat.name for chat in router.resolve(event)] == ["dev"]


def test_actions_filter_matches_merged_alias(config):
    config = make_config(
        defaults={"events": ["pull_request"], "ignore_actors": []},
        subscriptions=[{"repos": ["acme/api"], "chats": ["dev"], "actions": ["merged"]}],
    )
    router = Router(config)
    merged = normalize_webhook("pull_request", pull_request_payload(repo="acme/api", action="closed", merged=True))
    closed = normalize_webhook("pull_request", pull_request_payload(repo="acme/api", action="closed", merged=False))
    assert merged is not None and closed is not None
    assert router.resolve(merged) != []
    assert router.resolve(closed) == []


def test_actions_as_mapping_scopes_to_event_kind():
    """映射写法：只收窄 pull_request，不能连带把 issue_comment / release 丢掉。"""
    from larkbot.fixtures import issue_comment_payload, release_payload

    config = make_config(
        defaults={"events": ["pull_request", "issue_comment", "release"], "ignore_actors": []},
        subscriptions=[
            {
                "repos": ["acme/api"],
                "chats": ["dev"],
                "actions": {"pull_request": ["opened", "closed", "merged"]},
            }
        ],
    )
    router = Router(config)

    def hit(event_name, payload):
        event = normalize_webhook(event_name, payload)
        assert event is not None
        return bool(router.resolve(event))

    assert hit("pull_request", pull_request_payload(repo="acme/api", action="opened")) is True
    assert hit("pull_request", pull_request_payload(repo="acme/api", action="closed", merged=True)) is True
    assert hit("pull_request", pull_request_payload(repo="acme/api", action="labeled")) is False
    # 这两个事件的 action 是 created / published，不能被 PR 的列表误伤
    assert hit("issue_comment", issue_comment_payload(repo="acme/api")) is True
    assert hit("release", release_payload(repo="acme/api")) is True


def test_actions_mapping_ignores_unlisted_kinds():
    config = make_config(
        defaults={"events": ["push", "issues"], "ignore_actors": []},
        subscriptions=[{"repos": ["acme/api"], "chats": ["dev"], "actions": {"pull_request": ["opened"]}}],
    )
    router = Router(config)
    from larkbot.fixtures import issues_payload

    event = normalize_webhook("issues", issues_payload(repo="acme/api", action="labeled"))
    assert event is not None
    assert router.resolve(event) != []  # issues 没被列到 -> 不做 action 过滤


def test_actions_mapping_supports_wildcard_kind():
    config = make_config(
        defaults={"events": ["pull_request", "pull_request_review"], "ignore_actors": []},
        subscriptions=[
            {"repos": ["acme/api"], "chats": ["dev"], "actions": {"pull_request*": ["opened", "submitted"]}}
        ],
    )
    router = Router(config)
    review = {
        "action": "submitted",
        "review": {"id": 1, "state": "approved", "user": {"login": "alice"}},
        "pull_request": {"number": 9, "title": "x", "html_url": "https://x", "base": {"ref": "main"}},
        "repository": {"full_name": "acme/api"},
        "sender": {"login": "alice"},
    }
    submitted = normalize_webhook("pull_request_review", review)
    dismissed = normalize_webhook("pull_request_review", {**review, "action": "dismissed"})
    assert submitted is not None and dismissed is not None
    assert router.resolve(submitted) != []
    assert router.resolve(dismissed) == []


def test_actions_mapping_with_empty_patterns_does_not_filter():
    config = make_config(
        defaults={"events": ["pull_request"], "ignore_actors": []},
        subscriptions=[{"repos": ["acme/api"], "chats": ["dev"], "actions": {"pull_request": []}}],
    )
    router = Router(config)
    event = normalize_webhook("pull_request", pull_request_payload(repo="acme/api", action="labeled"))
    assert event is not None
    assert router.resolve(event) != []


def test_disabled_subscription_is_ignored(config):
    config = make_config(subscriptions=[{"repos": ["acme/*"], "chats": ["dev"], "enabled": False}])
    router = Router(config)
    assert router.resolve(_push("acme/api")) == []
    assert router.is_watched_repo("acme/api") is False


def test_disabled_chat_is_skipped(config):
    config = make_config(
        chats=[{"name": "dev", "webhook_url": "https://x", "enabled": False}],
        subscriptions=[{"repos": ["acme/*"], "chats": ["dev"]}],
    )
    router = Router(config)
    assert router.resolve(_push("acme/api")) == []


def test_concrete_repos_and_watched():
    config = make_config(
        subscriptions=[
            {"repos": ["acme/*"], "chats": ["dev"]},
            {"repos": ["acme/api", "octocat/Hello-World"], "chats": ["rel"], "events": ["release"]},
        ]
    )
    router = Router(config)
    assert router.concrete_repos() == ["acme/api", "octocat/Hello-World"]
    assert router.is_watched_repo("acme/anything") is True
    assert router.is_watched_repo("nope/nope") is False


def test_event_aliases():
    assert normalize_event_names(["pr", "PRs", "issue"]) == ["pull_request", "pull_request", "issues"]
    assert normalize_event_names(None) == []


# --- 指令创建的订阅（叠加语义）----------------------------------------------


def test_dynamic_subscription_adds_chat(config):
    """rel 在 config 里只收 release，指令给它加一个通用订阅后 push 也进。"""
    router = Router(config)
    event = _push("acme/api")
    assert [chat.name for chat in router.resolve(event)] == ["dev"]  # 指令前

    dynamic = {"rel": ["acme/api"]}
    assert [chat.name for chat in router.resolve(event, dynamic)] == ["dev", "rel"]


def test_dynamic_subscription_only_affects_its_chat(config):
    router = Router(config)
    dynamic = {"rel": ["acme/api"]}
    assert [chat.name for chat in router.resolve(_push("acme/other"), dynamic)] == ["dev"]


def test_dynamic_subscription_for_unknown_chat_is_ignored(config):
    router = Router(config)
    assert [chat.name for chat in router.resolve(_push("acme/api"), {"nope": ["acme/api"]})] == ["dev"]
    assert router.effective_subscriptions({"nope": ["acme/api"]}) == list(config.subscriptions)


def test_dynamic_subscription_inherits_defaults_filters(config):
    """指令创建的规则继承 defaults，所以机器人发的事件仍然被 *[bot] 过滤掉。"""
    router = Router(config)
    dynamic = {"rel": ["acme/api"]}
    assert router.resolve(_push("acme/api", actor="dependabot[bot]"), dynamic) == []


def test_dynamic_subscription_supports_wildcards(config):
    router = Router(config)
    dynamic = {"rel": ["acme/*"]}
    assert "rel" in [chat.name for chat in router.resolve(_push("acme/anything"), dynamic)]
    assert "rel" not in [chat.name for chat in router.resolve(_push("other/thing"), dynamic)]


def test_effective_subscriptions_counts_dynamic(config):
    router = Router(config)
    assert len(router.effective_subscriptions()) == len(config.subscriptions)
    assert len(router.effective_subscriptions({"rel": ["acme/api"]})) == len(config.subscriptions) + 1


def test_concrete_repos_and_watched_include_dynamic(config):
    router = Router(config)
    dynamic = {"rel": ["other/thing", "other/*"]}
    assert "other/thing" in router.concrete_repos(dynamic)
    assert "other/*" not in router.concrete_repos(dynamic)  # 通配不算具体仓库
    assert router.is_watched_repo("other/thing", dynamic) is True
    assert router.is_watched_repo("other/thing") is False


def test_static_patterns_and_known_patterns(config):
    router = Router(config)
    assert router.static_patterns_for_chat("rel") == ["acme/api"]
    assert router.known_repo_patterns() == ["acme/*", "acme/api"]
