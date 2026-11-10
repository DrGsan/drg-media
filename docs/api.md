# Private service API

The service API is for authenticated server-to-server integration, not direct public browser access. Preserve `api_version=1` compatibility. All archive routes require `Authorization: Bearer <service-token>`; the portal separately checks user/group permissions. `/healthz` is HTTP liveness only. Tokens and Telegram sessions remain on the server.

The table below is derived from the application route map. Request validation, object ownership and per-operation scope remain enforced by the implementation. Media routes support authorized byte-range retrieval; a source ID alone does not grant access.

| Methods | Route |
| --- | --- |
| GET | `/healthz` |
| GET | `/v1/admin/discovery` |
| POST | `/v1/admin/discovery` |
| GET | `/v1/admin/exports` |
| GET | `/v1/admin/groups` |
| POST | `/v1/admin/groups` |
| POST | `/v1/admin/groups/<gid>/<action>` |
| POST | `/v1/admin/groups/<gid>/copy` |
| POST | `/v1/admin/purged/ack` |
| GET | `/v1/exports` |
| POST | `/v1/exports` |
| GET | `/v1/exports/<jid>` |
| POST | `/v1/exports/<jid>/cancel` |
| GET | `/v1/exports/<jid>/download` |
| GET | `/v1/groups` |
| GET | `/v1/groups/<gid>/avatar` |
| GET | `/v1/groups/<gid>/emoji/<eid>` |
| GET | `/v1/groups/<gid>/media` |
| GET | `/v1/groups/<gid>/media-index` |
| GET | `/v1/groups/<gid>/media-version` |
| GET | `/v1/groups/<gid>/media/<fid>` |
| POST | `/v1/groups/<gid>/message-states` |
| GET | `/v1/groups/<gid>/messages` |
| POST | `/v1/groups/<gid>/messages` |
| GET | `/v1/groups/<gid>/messages/<int:mid>` |
| GET | `/v1/groups/<gid>/outbox` |
| GET | `/v1/groups/<gid>/outbox/<oid>` |
| GET | `/v1/groups/<gid>/people/<sid>/avatar` |
| PUT | `/v1/groups/<gid>/site-messages/<int:mid>` |
| GET | `/v1/groups/<gid>/site-sends` |
| POST | `/v1/groups/<gid>/site-sends` |
| GET | `/v1/groups/<gid>/site-sends/<jid>` |
| POST | `/v1/groups/<gid>/site-sends/<jid>/cancel` |
| PUT | `/v1/groups/<gid>/site-uploads/<fid>` |
| DELETE | `/v1/groups/<gid>/site-uploads/<fid>` |
| GET | `/v1/groups/<gid>/site-uploads/<fid>` |
| GET | `/v1/groups/<gid>/sync` |
| GET | `/v1/groups/<gid>/unread` |
| GET | `/v1/groups/<gid>/updates` |
| PUT | `/v1/site-permissions` |
| PUT | `/v1/site-reactions` |
| GET | `/v1/status` |
| GET | `/v1/storage` |
