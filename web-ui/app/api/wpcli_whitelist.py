"""Whitelist gating for the external /api/sites/{id}/wpcli passthrough.

docker_runner.wp_cli() passes args to subprocess as a real argv list (no
shell=True), so there is no shell-injection surface to defend against here —
this whitelist exists purely to bound *scope* for an API-key-authenticated
caller: block wp-cli verbs that are RCE-equivalent (eval, eval-file) or
SQLi-equivalent (db query, which can UNION SELECT arbitrary tables including
password hashes) even though they'd run confined to this one site's container.
"""

# None = all subcommands of that verb are allowed.
ALLOWED_WPCLI: dict[str, set[str] | None] = {
    "plugin": None,
    "theme": None,
    "option": {"get", "list", "pluck", "update", "delete"},
    "post": {"list", "get", "meta"},
    "user": {"list", "get", "meta"},
    "cache": None,
    "transient": None,
    "core": {"version", "is-installed", "verify-checksums", "update", "update-db"},
    "cron": {"event", "schedule"},
    "rewrite": None,
}


def check_wpcli_args(args: list[str]) -> str | None:
    """Returns a rejection reason if args should be blocked, else None."""
    if not args:
        return "指令不可為空"

    for a in args:
        if "\x00" in a:
            return "指令參數包含不允許的字元"

    verb = args[0]
    allowed_subs = ALLOWED_WPCLI.get(verb)
    if verb not in ALLOWED_WPCLI:
        return f"指令 '{verb}' 不在白名單中"

    if allowed_subs is not None:
        sub = args[1] if len(args) > 1 else ""
        if sub not in allowed_subs:
            return f"子指令 '{verb} {sub}' 不在白名單中"

    return None
