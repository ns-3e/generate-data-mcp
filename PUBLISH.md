# Publishing generate-data-mcp

The package at this path is publish-ready. Push to the public repo:

```bash
cd generate-data-mcp
git init
git add .
git commit -m "Initial public release: V2.0 + V2.1 MCP tools"
git branch -M main
git remote add origin https://github.com/ns-3e/generate-data-mcp.git
git push -u origin main
git tag v1.0.0
git push origin v1.0.0
```

Verify CI passes on GitHub Actions after push. Settings → API Access and `docs/API_V2.md` link to `https://github.com/ns-3e/generate-data-mcp`.
