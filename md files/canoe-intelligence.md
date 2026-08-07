# Canoe Intelligence -- Reference Notes for Automation

Source: Canoe help articles (PDF + extracted text) + api-docs-v1.json (OpenAPI 3.0)

## Authentication

### Two auth paths

| Path | Grant type | Use for |
|---|---|---|
| POST /v1/tokens | Password grant (username + password) | Interactive / manual scripts |
| POST /oauth/token/client-credentials | Client credentials (client_id + client_secret) | Automation (service accounts) |

**Primary recommendation:** client credentials with a service account.
- Created in Canoe Settings > API Configuration > Create New Client
- client_id + client_secret never expire individually (only when rotated by admin)
- Redirect URL is required but unused for client-credentials flow (enter a dummy URL)

### Token endpoint details (from JSON spec)

```
POST /oauth/token/client-credentials
Body: { grant_type, client_id, client_secret }
Headers: Accept: application/json, Content-Type: application/json
Response: { access_token, token_type, expires_in, scope }
```

### Token endpoint (password grant -- not recommended for automation)

```
POST /v1/tokens
Body (JSON): { username, password, organization_id? }
Response: { access_token, token_type, expires_in, scope }
```
- organization_id is required if the user has access to multiple organizations.

### Client / Management
```
GET /oauth/clients -- list clients owned by the authenticated user
```

### Token refresh (authorization code flow only)

```
POST /oauth/token/refresh
Body: { grant_type: refresh_token, refresh_token, client_id, client_secret }
```
Note: client-credentials flow does not use refresh tokens; just request a new token each time.
The token TTL is typically 1 hour (3600s) based on the expires_in field.

## Endpoint groups (39 endpoints per api-docs-v1.json)

### Authentication (2 endpoints)
- POST /v1/tokens -- password grant
- POST /oauth/token/client-credentials -- service account (RECOMMENDED)
- POST /oauth/token/refresh -- refresh for auth-code flow
- GET /oauth/clients -- list clients

### Documents (12 endpoints)
| Method | Path | Purpose |
|---|---|---|
| GET | /v1/documents | List documents (paginated) |
| GET | /v1/documents/{id} | Get a single document metadata |
| GET | /v1/documents/{id} | Download document bytes (same path, GET returns file) |
| POST | /v1/documents | Upload a single document |
| DELETE | /v1/documents/{id} | Delete a single document |
| POST | /v1/documents/delete-documents | Bulk delete |
| GET | /v1/documents/types | List document types |
| GET | /v1/documents/tags | List document tags |
| GET | /v1/documents/allocation-tags | List allocation tags |
| POST | /v1/documents/metadata | Bulk set metadata |
| PUT | /v1/documents/{id}/metadata | Set single document metadata |
| PATCH | /v1/documents/{allocationId}/allocation-tags | Update allocation tags |

### Documents on Funds (3 endpoints)
| Method | Path | Purpose |
|---|---|---|
| GET | /v1/funds/{id}/documents | Download all docs for a fund |
| GET | /v1/funds/{id}/document-ids | List document IDs for a fund |
| GET | /v1/funds/{id}/document-data | Get structured data from fund docs |

### Documents on Organizations (3 endpoints)
| Method | Path | Purpose |
|---|---|---|
| GET | /v1/organizations/{id}/documents | Download all org docs |
| GET | /v1/organizations/{id}/document-ids | List document IDs for an org |
| GET | /v1/organizations/{id}/document-data | Get structured data from org docs |

## Document listing -- key query parameters

| Parameter | Type | Description |
|---|---|---|
| page | integer | Page number (1-based) |
| limit | integer | Per-page count (max 100) |
| fund_id | string | Filter by fund |
| entity_id / investment_entity_id | string | Filter by organization/entity |
| document_type | string | Filter by document type |
| document_status | string | e.g. Ready for Extract |
| document_tag_id | string | Filter by tag IDs (comma separated for multiple) |
| not_document_tag_id | string | Exclude documents with these tag IDs |
| client_document_id | string | Exact match on client-provided ID |
| file_upload_time_start | string | ISO date -- new docs since |
| file_upload_time_end | string | ISO date |
| last_modified_time_start | string | ISO date |
| last_modified_time_end | string | ISO date |
| data_date_start | string | ISO date |
| data_date_end | string | ISO date |
| document_source | string | Filter by source |
| file_name_type | string | Filter by file naming convention |
| category | string | Filter by category |

## Rate limits
- 60 calls per minute per HTTP method (GET, POST, PUT, DELETE)
- Limits are per-method, not per-endpoint
- Unpaginated endpoints deprecated -- always include page + limit
- For delta polling, file_upload_time_start reduces result set size

## Headers (required on every request)
- Authorization: Bearer <access_token>
- Accept: application/json
- Content-Type: application/json
- X-Requested-With: XMLHttpRequest (some endpoints)

## API base URL
- Production: https://api.canoesoftware.com
- Ports: 443, 9443 (if IP whitelisting is required)

## IP Whitelisting
- Whitelist hostname: outgoing-ips.prod.canoesoftware.com
- Or request fixed IP range from Canoe support

## Discrepancies noted
- PDF help articles mention X-API-Version header; it does not appear in api-docs-v1.json
- PDF samples show ~8 endpoints; JSON has 39
- Use JSON spec as the authoritative source
