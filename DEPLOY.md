# Deploying the Lifesaver MCP server

One container, one Cloud Run service, serving the MCP endpoint over
streamable-http for a **claude.ai custom connector**.

```
claude.ai  ──HTTPS + Bearer token──▶  Cloud Run (1 instance)  ──▶  lsscloud.com
                                       mcp_server.server
                                       /mcp     (MCP, auth required)
                                       /health  (public)
```

**Why one instance:** LifeSaver allows one active session per user. The server
holds one login for the life of the process and serialises report pulls. Two
instances would fight over the session, so Cloud Run must be pinned to
`--max-instances=1`.

---

## 1. Image (GitHub Actions → GHCR)

`.github/workflows/publish.yml` runs the tests, then builds and pushes on every
push to `main` and every `v*` tag:

```
ghcr.io/ekufta0530/lifesaver-mcp:latest
ghcr.io/ekufta0530/lifesaver-mcp:sha-<commit>
ghcr.io/ekufta0530/lifesaver-mcp:v1.2.3   # on tags
```

The image is built for **`linux/amd64`** (Cloud Run's architecture). On an Apple
Silicon Mac, run it locally with `--platform linux/amd64`; a native arm64 build
hits a `cryptography` SIGILL under Docker Desktop (arm64-local only — CI and
Cloud Run are unaffected). For local dev just run `python -m mcp_server.server`
directly instead of the container.

No secrets are baked into the image. **Make the GHCR package public** so Cloud
Run can pull it without registry credentials:

> GitHub → repo → Packages → `lifesaver-mcp` → Package settings → Change
> visibility → Public.

<details><summary>Private image instead (Artifact Registry mirror)</summary>

Cloud Run can't pull a private GHCR image directly. Mirror it into Artifact
Registry and deploy from there:

```bash
gcloud artifacts repositories create lifesaver --repository-format=docker --location=us
docker pull ghcr.io/ekufta0530/lifesaver-mcp:latest
docker tag  ghcr.io/ekufta0530/lifesaver-mcp:latest \
            us-docker.pkg.dev/$PROJECT/lifesaver/mcp:latest
docker push us-docker.pkg.dev/$PROJECT/lifesaver/mcp:latest
```
</details>

---

## 2. Secrets (Secret Manager)

```bash
PROJECT=your-gcp-project
gcloud config set project $PROJECT
gcloud services enable run.googleapis.com secretmanager.googleapis.com

printf '%s' 'the-lifesaver-username' | gcloud secrets create lifesaver-username --data-file=-
printf '%s' 'the-lifesaver-password' | gcloud secrets create lifesaver-password --data-file=-
openssl rand -hex 32 | tr -d '\n'    | gcloud secrets create mcp-auth-token   --data-file=-

# note the token — you'll paste it into claude.ai
gcloud secrets versions access latest --secret=mcp-auth-token; echo
```

Let the Cloud Run runtime service account read them:

```bash
PROJNUM=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
for s in lifesaver-username lifesaver-password mcp-auth-token; do
  gcloud secrets add-iam-policy-binding $s \
    --member="serviceAccount:${PROJNUM}-compute@developer.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor
done
```

---

## 3. Deploy

```bash
gcloud run deploy lifesaver-mcp \
  --image=ghcr.io/ekufta0530/lifesaver-mcp:latest \
  --region=us-central1 \
  --allow-unauthenticated \
  --port=8080 \
  --max-instances=1 \
  --min-instances=1 \
  --concurrency=8 \
  --timeout=300 \
  --cpu=1 --memory=512Mi \
  --set-secrets=LIFESAVER_USERNAME=lifesaver-username:latest,LIFESAVER_PASSWORD=lifesaver-password:latest,MCP_AUTH_TOKEN=mcp-auth-token:latest
```

- `--allow-unauthenticated` opens it at the network layer — the app's own
  Bearer check is the real gate (claude.ai can't send a Google identity token).
- `--min-instances=1` keeps the login session warm and avoids a cold-start
  re-login on every call. Drop to `0` to save cost; the first call after idle
  then takes ~5–10s to log in (it auto-clears its own stale session).
- `--max-instances=1` is **required** (session limit).
- Optional hardening: after the URL is known, add
  `--set-env-vars=MCP_ALLOWED_HOSTS=lifesaver-mcp-xxxx-uc.a.run.app` to turn on
  DNS-rebinding protection (Host/Origin allow-list).

Redeploy after a new image build:

```bash
gcloud run services update lifesaver-mcp --region=us-central1 \
  --image=ghcr.io/ekufta0530/lifesaver-mcp:latest
```

---

## 4. Verify

```bash
URL=$(gcloud run services describe lifesaver-mcp --region=us-central1 --format='value(status.url)')
TOK=$(gcloud secrets versions access latest --secret=mcp-auth-token)

curl -sf "$URL/health"                       # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' "$URL/mcp"   # 401 (no token)

# MCP initialize handshake
curl -s "$URL/mcp" \
  -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

---

## 5. Add to claude.ai

Settings → Connectors → **Add custom connector**:

- **URL:** `https://lifesaver-mcp-xxxx-uc.a.run.app/mcp`
- **Authentication:** the connector needs to send `Authorization: Bearer <token>`
  using the `mcp-auth-token` value. If the UI only offers OAuth, use the
  "custom headers" option; otherwise this server would need an OAuth front end
  (not built — see spec.md).

Once connected, the `get_work_order_list_report` tool is available in chats.

---

## Rotating the token

```bash
openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add mcp-auth-token --data-file=-
gcloud run services update lifesaver-mcp --region=us-central1 \
  --update-secrets=MCP_AUTH_TOKEN=mcp-auth-token:latest
```

Then update the connector in claude.ai.
