# Mobotix Multipart Relay for Home Assistant

This service receives a multipart POST from a Mobotix T25, stores the camera
image, and rebuilds the request before forwarding it to Home Assistant. This
ensures a valid multipart boundary and a correct Content-Length header.

Flow:

```text
Mobotix T25 -> Relay:18425 -> Home Assistant webhook -> Phone notification
                         -> Image at /images/<random-name>.jpg
```

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

Then edit `.env`:

```dotenv
HA_WEBHOOK_URL=http://server:8123/api/webhook/YOUR_LONG_RANDOM_WEBHOOK_ID
PUBLIC_BASE_URL=
RELAY_PORT=18425
LOG_LEVEL=INFO
REQUEST_TIMEOUT=10
RETRY_COUNT=3
```

- `HA_WEBHOOK_URL` must contain the same webhook ID as the automation.
- `PUBLIC_BASE_URL` is normally empty because the relay derives it from the
  incoming request. Set it only when an explicit external URL is required.
- `RELAY_PORT` controls the port exposed on the Docker host.
- If the container cannot resolve `server`, use the Home Assistant IP address.

Docker Compose loads `.env` automatically. The file is excluded from Git and
the Docker build context because the webhook ID must remain private. Commit
`.env.example` as the configuration template, but never commit `.env`.

The relay automatically derives the image URL from the host used by the
Mobotix request. For example, a camera posting to
`http://192.168.111.10:18425/` produces image URLs on the same host and port.

Set `PUBLIC_BASE_URL` only when the generated address must differ, such as when
using an HTTPS reverse proxy or accessing images from outside the home network:

```dotenv
PUBLIC_BASE_URL=https://camera-relay.example.com
```

Start the service:

```sh
docker compose up -d --build
docker compose ps
docker compose logs -f mobotix-relay
```

The health status should become `healthy`. You can also check the endpoint
directly:

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
- `502 Home Assistant delivery failed`: Check the URL, webhook ID, DNS, and
  Home Assistant connectivity.
- Notification arrives without an image: Open the generated `image_url` from
  the phone and verify that the address is reachable. Configure
  `PUBLIC_BASE_URL` only if an override is required.
- View logs with `docker compose logs -f mobotix-relay`.
- Images remain in the `relay-images` Docker volume. There is no database or
  background worker.

Image URLs contain random file names but do not require authentication. Only
expose port `18425` on a trusted network, or publish it through a secured HTTPS
reverse proxy.