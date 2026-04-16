"""Gateway runner — extracted from cli.py for maintainability.

Contains _run_gateway (the multi-agent A2A gateway lifecycle) and
its helpers: _load_recipe_context, _build_auth_service, _handle_a2a_invocation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import click
import httpx
import uvicorn

from krewcli.agents.registry import AGENT_REGISTRY
from krewcli.client.krewhub_client import KrewHubClient
from krewcli.presence.heartbeat import HeartbeatLoop

logger = logging.getLogger(__name__)


def _get_owner_label() -> str:
    """Resolve a human-readable owner from the stored JWT."""
    try:
        from krewcli.auth.token_store import load_token
        import jwt as _pyjwt
        token = load_token()
        if not token:
            return "local"
        payload = _pyjwt.decode(token, options={"verify_signature": False})
        return payload.get("username") or payload.get("sub", "local")
    except Exception:
        return "local"


def _make_agent_id(name: str, owner: str) -> str:
    """Stable agent_id: name@owner (not port-dependent)."""
    return f"{name}@{owner}"


async def load_recipe_context(client: KrewHubClient, recipe_id: str) -> tuple[str, str]:
    """Fetch repo_url and branch for a recipe."""
    detail = await client.get_recipe(recipe_id)
    r = detail.get("recipe", {})
    return r.get("repo_url", ""), r.get("default_branch", "main")


def build_auth_service(settings):
    """Build auth service if JWT secret is configured."""
    if not settings.jwt_secret:
        logger.warning("KREWCLI_JWT_SECRET is not set — auth is DISABLED")
        return None
    if len(settings.jwt_secret) < 32:
        logger.warning("KREWCLI_JWT_SECRET is set but shorter than 32 chars — auth disabled")
        return None
    from krewcli.auth.service import AuthService
    logger.info("Auth enabled (JWT middleware active)")
    return AuthService(
        jwt_secret=settings.jwt_secret,
        token_expiry_minutes=settings.token_expiry_minutes,
    )


async def run_gateway(
    settings, recipe_id, cookbook_id, agent_id_prefix, working_dir,
    agent_names, max_concurrent,
):
    """Run the multi-agent A2A gateway."""
    import shutil

    from krewcli.a2a.gateway_server import create_gateway_app
    from krewcli.auth.token_store import load_token as _lt

    client = KrewHubClient(
        settings.krewhub_url, settings.api_key,
        jwt_token=_lt(), verify_ssl=settings.verify_ssl,
    )
    callback_url = f"{settings.krewhub_url}/api/v1/a2a/callback"

    repo_url, branch = await load_recipe_context(client, recipe_id)

    app, spawn_manager, registered_agents = create_gateway_app(
        host=settings.agent_host,
        port=settings.agent_port,
        working_dir=working_dir,
        repo_url=repo_url,
        branch=branch,
        callback_url=callback_url,
        api_key=settings.api_key,
        agent_names=agent_names,
        max_concurrent=max_concurrent,
        krewhub_client=client,
    )

    click.echo(f"  Agents: {', '.join(registered_agents)}")
    for name in registered_agents:
        click.echo(f"    /agents/{name} -> {name} CLI")

    # --- ERC-4337: session key + smart account ---
    from krewcli.auth.token_store import load_token as _load_token
    from krewcli.session_key import load_session_key, get_session_key_address

    _jwt = _load_token()
    erc8004_ids: dict[str, int] = {}
    session_addr = get_session_key_address()

    if _jwt and session_addr:
        auth_url = settings.krew_auth_url

        try:
            acct_resp = await asyncio.to_thread(
                lambda: httpx.get(f"{auth_url}/auth/account/info", params={"token": _jwt}, timeout=10).json()
            )
            smart_addr = acct_resp.get("smart_address")
            click.echo(f"\n  Smart Account: {smart_addr}")
            click.echo(f"  Session Key: {session_addr}")

            if smart_addr:
                for name in registered_agents:
                    display_name, capabilities = _gateway_agent_metadata(name)
                    try:
                        req_resp = await asyncio.to_thread(lambda n=name, dn=display_name: httpx.post(
                            f"{auth_url}/auth/session-keys/request",
                            json={
                                "token": _jwt,
                                "agent_name": n,
                                "session_pubkey": session_addr,
                                "allowed_targets": [settings.erc8004_identity_registry],
                                "allowed_selectors": ["0xf2c298be"],
                                "spend_limit": "0",
                                "valid_hours": 24,
                            },
                            timeout=10,
                        ).json())
                        click.echo(f"  Session key requested for {dn}: {req_resp.get('status', 'unknown')}")
                    except Exception as e:
                        click.echo(f"  Session key request failed for {name}: {e}")

                click.echo(f"\n  Approve session keys in cookrew to enable on-chain operations.")
                click.echo(f"  Off-chain operations (task claims, events) work immediately via JWT.")

                try:
                    from krewcli.mint_agents import build_one_click_userop
                    owner = _get_owner_label()
                    mint_ops = []
                    for name in registered_agents:
                        display_name, capabilities = _gateway_agent_metadata(name)
                        op = await asyncio.to_thread(lambda n=name, dn=display_name: build_one_click_userop(
                            agent_name=n,
                            display_name=dn,
                            owner_address=acct_resp.get("owner_address", ""),
                            session_key_addr=session_addr,
                            identity_registry=settings.erc8004_identity_registry,
                            factory_address=settings.erc8004_identity_registry,
                            entrypoint_address="0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789",
                            rpc_url=settings.erc8004_rpc_url,
                            hub_base_url=settings.krewhub_url,
                            owner_name=owner,
                        ))
                        mint_ops.append(op)

                    mint_resp = await asyncio.to_thread(lambda: httpx.post(
                        f"{auth_url}/auth/mint-ops/submit",
                        json={"token": _jwt, "ops": [
                            {"agent_name": op["agent_name"], "display_name": op["display_name"],
                             "agent_uri": op["agent_uri"], "smart_account": op["smart_account"],
                             "userop": op["userop"]}
                            for op in mint_ops
                        ]},
                        timeout=30,
                    ).json())
                    click.echo(f"  {mint_resp.get('detail', 'Mint ops submitted')}")
                    click.echo(f"  Open cookrew → click [Mint] next to each agent")
                except Exception as e:
                    click.echo(f"  Mint setup skipped: {e}")
            else:
                click.echo(f"  No smart account — connect wallet in cookrew first")
        except Exception as e:
            click.echo(f"\n  Account lookup failed: {e}")
    elif _jwt:
        click.echo("\n  No session key — run 'krewcli session-key create' first")
    else:
        click.echo("\n  No session — run 'krewcli login' first")

    # Register each agent type in krewhub
    heartbeats: list[HeartbeatLoop] = []
    _owner_label = _get_owner_label()
    for name in registered_agents:
        agent_id = _make_agent_id(name, _owner_label)
        endpoint_url = f"http://{settings.agent_host}:{settings.agent_port}/agents/{name}"

        entry = AGENT_REGISTRY.get(name, {})
        display_name = entry.get("display_name", name)
        capabilities = entry.get("capabilities", [])

        try:
            await client.register_agent(
                agent_id=agent_id,
                cookbook_id=cookbook_id,
                display_name=display_name,
                capabilities=capabilities,
                max_concurrent_tasks=max_concurrent,
                endpoint_url=endpoint_url,
            )
            erc_tag = f" (ERC-8004 #{erc8004_ids[name]})" if name in erc8004_ids else ""
            click.echo(f"  Registered {display_name} ({agent_id}){erc_tag}")
        except Exception:
            logger.warning("Registration failed for %s, continuing with heartbeat", name)

        hb = HeartbeatLoop(
            client=client,
            agent_id=agent_id,
            cookbook_id=cookbook_id,
            display_name=display_name,
            capabilities=capabilities,
            interval=settings.heartbeat_interval,
            endpoint_url=endpoint_url,
        )
        hb.start()
        heartbeats.append(hb)

    # Start SSE watcher for A2A invocations from hub gateway
    from krewwatch import SSEWatcher
    from krewcli.workflows.llm_planner import CODEGEN_PROMPT

    owner = _owner_label

    async def _handle_a2a_invocation(payload: dict) -> dict | None:
        """Bridge: krewhub A2A invocation → local agent executor → real result."""
        agent_name = payload.get("agent_name", "")
        message = payload.get("message", "")
        params = payload.get("params", {})

        msg_obj = params.get("message", {})
        parts = msg_obj.get("parts", [])
        text = "\n".join(p.get("text", "") for p in parts if "text" in p) or message
        metadata = msg_obj.get("metadata", {})

        if not text:
            text = message or json.dumps(params)

        click.echo(f"  A2A invocation: {agent_name} ← {text[:80]}")

        if agent_name not in registered_agents:
            return {"text": f"Agent '{agent_name}' not registered on this gateway"}

        bundle_id = metadata.get("bundle_id")
        task_id_from_meta = metadata.get("task_id")
        if bundle_id and not task_id_from_meta:
            return await _handle_planner_task(
                client, spawn_manager, agent_name, text,
                bundle_id, cookbook_id, working_dir, repo_url, branch,
            )

        task_id = metadata.get("task_id")
        recipe_id_meta = metadata.get("recipe_id", "")
        bundle_id_meta = metadata.get("bundle_id", "")
        return await _handle_regular_task(
            client, spawn_manager, agent_name, text,
            task_id, recipe_id_meta, bundle_id_meta, working_dir, repo_url, branch,
            settings=settings,
        )

    sse_watcher = SSEWatcher(
        krewhub_url=settings.krewhub_url,
        jwt_token=_lt() or "",
        owner=owner,
        agent_names=[n for n in registered_agents],
        on_invocation=_handle_a2a_invocation,
        token_reloader=_lt,
    )
    sse_watcher.start()

    await sse_watcher.poll_pending()

    click.echo(f"\nGateway ready.")
    click.echo(f"  A2A: hub.cookrew.dev/a2a/{owner}/*")
    click.echo(f"  Listening for tasks + A2A invocations via SSE...")

    config = uvicorn.Config(
        app, host=settings.agent_host, port=settings.agent_port, log_level="info"
    )
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()
    _orig_handler = loop.get_exception_handler()

    def _shutdown_exception_handler(loop, context):
        exc = context.get("exception")
        if isinstance(exc, (asyncio.InvalidStateError, OSError, BrokenPipeError)):
            return
        if _orig_handler:
            _orig_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    try:
        await server.serve()
    finally:
        loop.set_exception_handler(_shutdown_exception_handler)
        await spawn_manager.shutdown()
        for hb in heartbeats:
            try:
                await hb.stop()
            except (asyncio.CancelledError, asyncio.InvalidStateError, OSError):
                pass
        try:
            await client.close()
        except (asyncio.CancelledError, asyncio.InvalidStateError, OSError):
            pass


def _gateway_agent_metadata(name: str) -> tuple[str, list[str]]:
    """Look up display name and capabilities for a registered agent."""
    entry = AGENT_REGISTRY.get(name, {})
    display_name = entry.get("display_name", name)
    capabilities = entry.get("capabilities", [])
    return display_name, capabilities


async def _handle_planner_task(
    client, spawn_manager, agent_name, text,
    bundle_id, cookbook_id, working_dir, repo_url, branch,
) -> dict:
    """Handle a planner task: generate graph code and attach to bundle."""
    click.echo(f"  Planner task: generating graph for bundle {bundle_id}")
    try:
        from krewcli.workflows.llm_planner import CODEGEN_PROMPT

        agents_list = await client.list_agents(cookbook_id)
        agent_summary = ", ".join(
            a.get("display_name", a.get("agent_id", "?")) for a in agents_list
        )
        codegen_prompt = CODEGEN_PROMPT.format(prompt=text, agents=agent_summary)

        click.echo(f"  Running {agent_name} for graph codegen...")
        # Planner codegen has no task scope — events have nowhere to
        # post. Use a null sink explicitly for intent. If bundle-scoped
        # streaming is added server-side later, swap this for a
        # BundleEventSink.
        sink = spawn_manager.build_task_event_sink(task_id="", agent_id=agent_name)
        spawn_result = await spawn_manager._execute(
            agent_name=agent_name,
            prompt=codegen_prompt,
            working_dir=working_dir,
            repo_url=repo_url,
            branch=branch,
            event_sink=sink,
        )

        if not spawn_result.success:
            return {"text": f"Graph codegen failed: {spawn_result.blocked_reason or spawn_result.summary}", "success": False}

        output = spawn_result.full_output or spawn_result.summary or ""
        fence_re = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)
        matches = fence_re.findall(output)
        code = None
        for m in matches:
            if "g.build()" in m or "graph = " in m:
                code = m.strip()
                break
        if not code and ("g.build()" in output or "graph = " in output):
            code = output.strip()

        if not code:
            return {"text": "Agent output did not contain valid graph code", "success": False}

        click.echo(f"  Attaching graph ({len(code)} bytes) to bundle {bundle_id}")
        result = await client.attach_graph(bundle_id, code, created_by="orchestrator")
        task_count = len(result.get("tasks", []))
        click.echo(f"  Graph attached: {task_count} tasks created")
        return {
            "text": f"Graph attached: {task_count} tasks created",
            "success": True,
            "bundle_id": bundle_id,
            "task_count": task_count,
        }
    except Exception as e:
        click.echo(f"  Planner error: {e}")
        return {"text": f"Graph generation failed: {e}", "success": False}


async def _handle_regular_task(
    client, spawn_manager, agent_name, text,
    task_id, recipe_id, bundle_id, working_dir, repo_url, branch,
    *, settings=None,
) -> dict:
    """Handle a regular task: run agent CLI and update krewhub task status.

    Streams execution events (tool_use, thinking, session_*) to krewhub
    via two channels:
      - event_sink: used by agents that emit via sink.emit() (claude_agent)
      - deps.context: used by codex_agent's rollout watcher, which
        forwards events via bridge/forwarder.py using KREWHUB_TASK_ID /
        KREWHUB_URL / KREWHUB_API_KEY env keys.

    Without both, the UI only sees the terminal status transition
    (claimed → done) with no intermediate activity.
    """
    # Build a sink up front so a `finally` flush is always safe, even
    # if _execute raises before assigning to spawn_result.
    sink = spawn_manager.build_task_event_sink(task_id=task_id or "", agent_id=agent_name)

    # Context for CLI-backed agents (codex). bridge/forwarder.py reads
    # these keys from env to route events to the right task in krewhub.
    context: dict[str, str] = {}
    if task_id:
        context["KREWHUB_TASK_ID"] = task_id
    if recipe_id:
        context["KREWHUB_RECIPE_ID"] = recipe_id
    if bundle_id:
        context["KREWHUB_BUNDLE_ID"] = bundle_id
    if settings is not None:
        if getattr(settings, "krewhub_url", None):
            context["KREWHUB_URL"] = settings.krewhub_url
        if getattr(settings, "api_key", None):
            context["KREWHUB_API_KEY"] = settings.api_key

    try:
        if task_id:
            try:
                await client.update_task_status(task_id, "working")
            except Exception:
                pass

        spawn_result = await spawn_manager._execute(
            agent_name=agent_name,
            prompt=text,
            working_dir=working_dir,
            repo_url=repo_url,
            branch=branch,
            event_sink=sink,
            context=context,
        )

        if spawn_result.success:
            click.echo(f"  A2A result: {agent_name} → {spawn_result.summary[:80]}")
            if task_id:
                try:
                    await client.update_task_status(task_id, "done")
                    await client.post_recipe_event(
                        recipe_id=recipe_id,
                        event_type="milestone",
                        actor_id=agent_name,
                        body=spawn_result.summary or "Task completed",
                        payload={
                            "task_id": task_id,
                            "files_modified": spawn_result.files_modified,
                            "code_refs": spawn_result.code_refs,
                        },
                    )
                except Exception as e:
                    click.echo(f"  Warning: failed to update task status: {e}")
            return {
                "text": spawn_result.summary or spawn_result.full_output,
                "success": True,
                "files_modified": spawn_result.files_modified,
                "code_refs": spawn_result.code_refs,
            }
        else:
            reason = spawn_result.blocked_reason or spawn_result.summary or "Agent failed"
            click.echo(f"  A2A failed: {agent_name} → {reason}")
            if task_id:
                try:
                    await client.update_task_status(task_id, "blocked", blocked_reason=reason)
                except Exception:
                    pass
            return {"text": reason, "success": False}

    except Exception as e:
        click.echo(f"  A2A error: {agent_name} → {e}")
        if task_id:
            try:
                await client.update_task_status(task_id, "blocked", blocked_reason=str(e))
            except Exception:
                pass
        return {"text": f"Error: {e}", "success": False}
    finally:
        # Drain buffered tool_use / thinking / session events so the
        # UI sees the full run, not just the terminal status update.
        try:
            await sink.flush()
        except Exception:
            logger.exception("event sink flush failed for task %s", task_id)
