# Gateway Restart Request

Profile-local standalone plugin for exactly `general` and `coder`.

- Registers `request_gateway_restart`, a narrow model-facing facade over the external Restart Coordinator.
- Derives requester authorization from the existing facade (`HERMES_HOME` plus live gateway ancestry); there is no requester argument.
- Blocks model-driven direct `hermes gateway restart` and gateway-targeting `launchctl` mutations through `pre_tool_call`.
- Does not intercept the human `/restart` command; that remains documented emergency fallback only.
- Installs into no other profile and does not mutate coordinator state or restart gateways.

## Test

```bash
python3 -m unittest components/operations/gateway-restart-request/tests/test_gateway_restart_request.py -v
```

## Install

```bash
python3 components/operations/gateway-restart-request/install_gateway_restart_request.py
python3 components/operations/gateway-restart-request/install_gateway_restart_request.py --apply
```

A gateway restart is required for `general` and `coder` to load the plugin. Use the existing external coordinator for activation.
