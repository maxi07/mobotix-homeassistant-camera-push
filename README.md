# Mobotix Multipart Relay for Home Assistant

This service receives a multipart POST from a Mobotix T25, stores the camera
image, and rebuilds the request before forwarding it to Home Assistant. This
ensures a valid multipart boundary and a correct Content-Length header.

Flow:

```text
Mobotix T25 -> Relay:18425 -> Home Assistant webhook -> Phone notification
```

![Home Assistant Push Notification](/documentation/home-assistant-screenshot.jpeg)

## Requirements

- Docker with Docker Compose
- Home Assistant running at `server:8123`
- The Home Assistant Companion app installed on the target phone
- Network connectivity between the camera, relay, Home Assistant, and phone

## 1. Configure Home Assistant

Open **Settings > Automations & Scenes**, create an automation, select
**Edit in YAML**, and enter:

```yaml
alias: Mobotix Front Door
description: Sends a notification with the image received from the T25
triggers:
  - trigger: webhook
    webhook_id: YOUR_LONG_RANDOM_WEBHOOK_ID
    allowed_methods:
      - POST
    local_only: true
conditions: []
actions:
  - action: notify.mobile_app_YOUR_PHONE
    data:
      title: Front door
      message: >-
        {{ trigger.data.message | default('Motion detected', true) }}
      data:
        image: "{{ trigger.data.image_url }}"
mode: queued
max: 10
```

Replace the following values:

- `YOUR_LONG_RANDOM_WEBHOOK_ID`: Use a non-guessable value such as a random
  UUID. Treat this ID like a password.
- `notify.mobile_app_YOUR_PHONE`: Find the actual notification action under
  **Developer Tools > Actions**.

`local_only: true` is appropriate when the relay reaches Home Assistant over
the local network. Adjust this setting and your network security if the relay
runs outside the local network.

The resulting webhook URL is:

```text
http://server:8123/api/webhook/YOUR_LONG_RANDOM_WEBHOOK_ID
```

The notification displays the title **Front door**, the message supplied by
the camera, and the current camera image.

## 2. Configure the relay

Create the local environment file from the included template:

```sh
cp .env.example .env
```

The relay offers two storage backends, selected with `STORAGE_BACKEND`:

| Backend | Image reachable | Use when |
| --- | --- | --- |
| `local` (default) | only on the local network | phones are always at home, or you already expose Home Assistant remotely |
| `r2` | anywhere | the notification must show the image away from home, **without** exposing your server |

### Option A: `local` backend (default)

The relay stores images and serves them itself:

```dotenv
HA_WEBHOOK_URL=http://server:8123/api/webhook/YOUR_LONG_RANDOM_WEBHOOK_ID
STORAGE_BACKEND=local
PUBLIC_BASE_URL=
IMAGE_RETENTION_MINUTES=60
RELAY_PORT=18425
```

- `HA_WEBHOOK_URL` must contain the same webhook ID as the automation.
- `PUBLIC_BASE_URL` is normally empty because the relay derives it from the
  incoming request. Set it only when an explicit external URL is required.
- `IMAGE_RETENTION_MINUTES` deletes stored images after the given time. Set `0`
  to keep them indefinitely.
- `RELAY_PORT` controls the port exposed on the Docker host.
- If the container cannot resolve `server`, use the Home Assistant IP address.

The relay derives the image URL from the host used by the Mobotix request. A
camera posting to `http://192.168.111.10:18425/` produces image URLs on the
same host and port.

> [!WARNING]
> Setting `PUBLIC_BASE_URL` to a public address publishes the images **without
> authentication**. Random filenames are obscurity, not access control. Prefer
> the `r2` backend for remote access.

### Option B: `r2` backend (image visible away from home)

The Home Assistant Companion app downloads a notification image **on the
phone**, at the moment the push arrives. A LAN address therefore only works
while the phone is at home.

This backend uploads the image to Cloudflare R2 and hands Home Assistant a
**presigned URL** that expires after a short window:

```text
Mobotix -> Relay -> R2 upload -> presigned URL -> HA webhook -> phone
```

The relay only makes **outbound** connections. No port is opened, no tunnel is
required, and Home Assistant stays unreachable from the internet.

```dotenv
HA_WEBHOOK_URL=http://server:8123/api/webhook/YOUR_LONG_RANDOM_WEBHOOK_ID
STORAGE_BACKEND=r2
R2_ACCOUNT_ID=your_cloudflare_account_id
R2_BUCKET=doorbell
R2_ACCESS_KEY_ID=your_access_key_id
R2_SECRET_ACCESS_KEY=your_secret_access_key
R2_JURISDICTION=eu
URL_TTL_SECONDS=900
```

- `R2_JURISDICTION=eu` targets a bucket created with EU jurisdiction and
  selects the `<account>.eu.r2.cloudflarestorage.com` endpoint. Leave empty for
  a standard bucket.
- `URL_TTL_SECONDS` controls how long the presigned URL stays valid. The relay
  deletes the object after the same period.
- `R2_KEY_PREFIX` optionally stores objects under a prefix.
- `R2_ENDPOINT_URL` overrides the endpoint entirely, which makes this backend
  usable with any S3-compatible service (AWS S3, MinIO, Backblaze B2).

**Setting up the bucket in Cloudflare:**

1. **R2 > Create bucket.** Pick a name, and under *Location* choose
   *Specify jurisdiction > European Union (EU)* if you need EU data residency.
   This cannot be changed later — location hints are only honoured the first
   time a bucket of that name is created.
2. **R2 > Manage API Tokens > Create.** Permission **Object Read & Write**,
   scoped to that single bucket. Note the Access Key ID and Secret.
3. **Bucket > Settings > Object Lifecycle Rules.** Add *Delete objects* after
   **1 day**. This is a safety net for objects the relay fails to delete after
   a crash or restart; the actual retention comes from `URL_TTL_SECONDS`.

Leave *Public Access* disabled. The bucket stays private — an unsigned request
is rejected with `401`.

> [!NOTE]
> Objects are encrypted at rest with AES-256 automatically, and transferred
> over HTTPS. Server-side encryption with customer keys (SSE-C) is **not**
> usable here: the Companion app cannot send the decryption header when
> fetching an attachment. For the same reason Basic Auth and Cloudflare Access
> cannot protect the image — the presigned URL is the practical option.

> [!WARNING]
> A presigned URL is a bearer credential: anyone holding the complete link can
> fetch the image until it expires. The link travels inside the push payload
> through Apple or Google. Keep `URL_TTL_SECONDS` short.

R2 lifecycle rules operate in **days** and objects are removed "typically
within 24 hours" of expiry, so a 15-minute retention cannot come from a
lifecycle rule. The relay therefore deletes objects itself once their URL has
expired.

### Starting the service

Docker Compose loads `.env` automatically. The file is excluded from Git and
the Docker build context because the webhook ID and R2 credentials must remain
private. Commit `.env.example` as the configuration template, but never commit
`.env`.

```sh
docker compose up -d --build
docker compose ps
docker compose logs -f mobotix-relay
```

The log line `Storage backend=... retention=...s` confirms which backend is
active. The health status should become `healthy`. You can also check the
endpoint directly:

```sh
curl http://localhost:18425/healthz
```

Expected response:

```json
{"status":"ok"}
```

## 3. Configure the Mobotix T25

Create an HTTP notification profile in the Mobotix configuration:

- Target: `http://DOCKER_HOST_IP:18425/`
- Method: `POST`
- Content type: `multipart/form-data`
- Include the camera image, preferably as JPEG
- Set the image part Content-Type to a value such as `image/jpeg`
- Optionally include a text field named `message`

![Mobotix Config](/documentation/mobotix.png)

The image field name can be arbitrary. The relay uses the first multipart field
with an `image/*` Content-Type and forwards all text fields unchanged.

The relay also supports the T25's native `mxmsg/2.2` format: HTTP/1.0 requests
without `Content-Length`, bodies terminated by connection close, MIME parts
without `Content-Disposition`, and image parts using
`application/octet-stream`. JPEG, PNG, GIF, and WebP are detected by their file
signatures.

## 4. Test without the camera

Use a local JPEG file to test the complete flow:

```sh
curl --fail-with-body \
  -F "message=Front door test" \
  -F "image=@test.jpg;type=image/jpeg" \
  http://localhost:18425/
```

On success, the relay responds with `status: delivered`, Home Assistant runs
the automation, and the phone receives a notification with the image.

## Data available in Home Assistant

The webhook receives multipart form data. These values are available in
templates:

| Template | Value |
| --- | --- |
| `trigger.data.message` | Message supplied by the camera |
| `trigger.data.image_url` | URL of the stored image |
| `trigger.data.source_ip` | Camera IP address |
| `trigger.data.received_at` | Reception time in ISO format |

## Operations and troubleshooting

- `415 multipart/form-data is required`: The camera did not send a multipart
  request.
- `400 multipart image is required`: No part with an `image/*` Content-Type was
  included and no supported image signature was detected. Check the
  `body_length`, `files`, and `form_fields` values in the preceding log entry.
- `502 storing the image failed`: The storage backend rejected the upload.
  With `STORAGE_BACKEND=r2`, check the credentials, bucket name and
  jurisdiction. The preceding log entry names the underlying error.
- `502 Home Assistant delivery failed`: Check the URL, webhook ID, DNS, and
  Home Assistant connectivity.
- `STORAGE_BACKEND=r2 requires: ...` on startup: one or more R2 settings are
  missing from `.env`. The message lists exactly which.
- Notification arrives without an image while away from home: the `local`
  backend cannot work outside the LAN. Switch to `STORAGE_BACKEND=r2`.
- Notification arrives without an image at home: open the generated
  `image_url` from the phone and verify that the address is reachable.
- iOS shows no thumbnail with the `r2` backend: presigned URLs carry no file
  extension. Add an explicit content type in the automation:

  ```yaml
  data:
    image: "{{ trigger.data.image_url }}"
    attachment:
      content-type: jpeg
  ```

- View logs with `docker compose logs -f mobotix-relay`.
- With `STORAGE_BACKEND=local`, images are kept in the `relay-images` Docker
  volume and removed after `IMAGE_RETENTION_MINUTES`. With `r2`, objects live
  in the bucket until their presigned URL expires. There is no database or
  background worker beyond a small expiry sweeper.

With the `local` backend, image URLs contain random file names but do not
require authentication. Only expose port `18425` on a trusted network. For
remote access use the `r2` backend rather than a public reverse proxy.