# Devin MCP server (optional)

Separately from this plugin, Hermes can talk to Devin's hosted MCP server through its own MCP client. That server exposes Devin's cloud features, such as Devin sessions, the wiki and repository questions, as tools.

The two do different jobs and can be used together:

| | Hermes Devin (this plugin) | Devin MCP server |
| --- | --- | --- |
| Runs | The Devin CLI on your machine, in a folder you name | Devin's cloud service |
| Auth | Your `devin auth login` session | A Devin API key |
| Approval | Hermes approval prompt for every run | Hermes' normal MCP tool handling |

## Setup

You need a Devin API key that starts with `cog_`. Legacy `apk_` keys do not work. Keep the key in your environment or Hermes' `.env`, not in `config.yaml`.

```yaml
mcp_servers:
  devin:
    url: https://mcp.devin.ai/mcp
    headers:
      Authorization: "Bearer ${DEVIN_API_KEY}"
      # X-Org-Id: "${DEVIN_ORG_ID}"   # only for enterprise keys and personal access tokens
```

Restart Hermes after adding it. The MCP tools appear under their own toolset.

This plugin does not need the MCP server and does not use it.
