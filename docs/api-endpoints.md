# SurveyCTO endpoints (verified 2026-10-02)

Base URL: `https://{server}.surveycto.com`, where `{server}` is the bare subdomain in
`SCTO_SERVER`. Every call uses HTTP Basic auth. Form data is pulled as wide JSON.

| Purpose | Request | Notes |
|---|---|---|
| Form list | `GET /formList` | OpenRosa XML. Each `<xform>` has `formID`, `version`, `hash`, `downloadUrl`, `manifestUrl`, and SurveyCTO extras (`datasets`, `files`, `groupId`, `ordinal`, `publishingLinks`, `majorMinorVersion`). |
| Compiled XForm | `GET /forms/{form_id}/xml` | Byte-identical to the form list's `downloadUrl` (`/forms/{form_id}/xml?t=<ms>`). |
| Form attachments manifest | `GET /forms/{form_id}/manifest` | OpenRosa manifest XML; one `mediaFile` (`filename`, `hash`, `downloadUrl`) per attachment. |
| Wide JSON, decrypted | `POST /api/v2/forms/data/wide/json/{form_id}?date=<epoch s>` | Private key as multipart file field `private_key`. `date` = oldest `CompletionDate` to return, in epoch seconds; `date=0` is throttled to once per 300 s, so `date=1` means "all". |
| Wide JSON, no key | `GET /api/v2/forms/data/wide/json/{form_id}?date=<epoch s>` | On an encrypted form returns only publishable fields plus a fixed set of system columns. |
| Server dataset | `GET /api/v2/datasets/data/csv/{dataset_id}` | CSV. |
