import pytest

from twscrape.xclid import INDICES_FILE_RE, get_scripts_list

BASE = "https://abs.twimg.com/responsive-web/client-web"
PRELOAD = f'<link rel="preload" as="script" href="{BASE}/main.67fd5623c070a504a.js" />'

# 2026-10 build: builder lost its leading `""+`
BUILDER_2026_10 = (
    't.u=e=>(({346:"bundle.NotABot",59924:"ondemand.s",99969:"bundle.Notifications"})[e]||e)'
    '+"."+({346:"05aa11bb22cc33dd",59924:"c93af3b19aab5e67",99969:"b82990d5d7f26126"})[e]+"a.js"'
)
# older build: `X.u=e=>""+((...` with 7-hex hashes
BUILDER_LEGACY = (
    'X.u=e=>""+(({59924:"ondemand.s",20113:"bundle.Foo"})[e]||e)'
    '+"."+({59924:"abc1234",20113:"def5678"})[e]+"a.js"'
)


@pytest.mark.parametrize(
    "builder,expected",
    [
        (BUILDER_2026_10, f"{BASE}/ondemand.s.c93af3b19aab5e67a.js"),
        (BUILDER_LEGACY, f"{BASE}/ondemand.s.abc1234a.js"),
    ],
)
def test_get_scripts_list_webpack(builder, expected):
    urls = get_scripts_list(PRELOAD + f"<script>{builder}</script>")
    assert f"{BASE}/main.67fd5623c070a504a.js" in urls
    assert expected in urls
    assert [u for u in urls if INDICES_FILE_RE.search(u)] == [expected]


def test_get_scripts_list_x_web():
    url = "https://abs.twimg.com/x-web/assets/entry-client-abc.js"
    assert get_scripts_list(f'<script type="module" src="{url}"></script>') == [url]


def test_get_scripts_list_errors():
    with pytest.raises(Exception, match="Cloudflare"):
        get_scripts_list('<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/x"></script>')
    with pytest.raises(Exception, match="Failed to parse scripts"):
        get_scripts_list("<html></html>")
