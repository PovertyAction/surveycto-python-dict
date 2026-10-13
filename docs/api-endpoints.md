# SurveyCTO API: what the tool calls and what comes back

Base URL: `https://{server}.surveycto.com`, where `{server}` is the bare subdomain in
`SCTO_SERVER`. Every call uses HTTP Basic auth. Form data is pulled as wide JSON. Each
shape below was checked against live servers; the test fixtures reproduce them with
invented forms.

## Endpoints

| Purpose | Request | Notes |
|---|---|---|
| Form list | `GET /formList` | OpenRosa XML. Each `<xform>` has `formID`, `version`, `hash`, `downloadUrl`, `manifestUrl`, and SurveyCTO extras (`datasets`, `files`, `groupId`, `ordinal`, `publishingLinks`, `majorMinorVersion`). `publishingLinks` is empty even for forms that publish a dataset, so the build infers publishers from the datasets' columns. |
| Compiled XForm | `GET /forms/{form_id}/xml` | Byte-identical to the form list's `downloadUrl` (`/forms/{form_id}/xml?t=<ms>`). |
| Form attachments manifest | `GET /forms/{form_id}/manifest` | OpenRosa manifest XML; one `mediaFile` (`filename`, `hash`, `downloadUrl`) per attachment. Each attached CSV is served zipped: `prices.csv` is the single member of `prices.zip`. |
| Wide JSON, decrypted | `POST /api/v2/forms/data/wide/json/{form_id}?date=<epoch s>` | Private key as multipart file field `private_key`. `date` = oldest `CompletionDate` to return, in epoch seconds; `date=0` is throttled to once per 300 s, so `date=1` means "all". |
| Wide JSON, no key | `GET /api/v2/forms/data/wide/json/{form_id}?date=<epoch s>` | On an encrypted form, returns only the publishable fields plus a fixed set of system columns (below). |
| Server dataset list | `GET /api/v2/datasets` | JSON `{total, limit, data, nextCursor}`; one page of `data` per call, the next with `?cursor=<nextCursor>` until it is null. Each entry: `id`, `title`, `discriminator`, `groupId`, `uniqueRecordField`, `fieldNames`, `totalRecords`, dates. |
| Server dataset | `GET /api/v2/datasets/data/csv/{dataset_id}` | CSV, kept as the server's bytes. |

## Wide JSON shape

- Every value is a JSON string, and each row is a flat object.
- **Keys are sparse per row.** A field that was not relevant or not selected is absent
  from the row, not `""`. Casting reads an absent key as empty (null).
- **A select_multiple comes as per-choice binaries** `<field>_<code>`, with no
  space-separated `<field>` column. A binary is present, with value `"1"`, only when
  that choice was picked; the build fills `0` for the other choices of an answered row.
- `dateTime` fields and system timestamps read `Mon D, YYYY H:MM:SS AM` (for example
  `Mar 7, 2025 4:06:12 PM`), in UTC
  ([SurveyCTO: exporting in different time zones](https://support.surveycto.com/hc/en-us/articles/360046966333-Exporting-data-in-different-time-zones));
  the parquet types them `timestamp[ms, tz=UTC]`.

## Encrypted forms

- **`publishable` reaches the compiled XML** as the bind attribute
  `publishable="true()"`. A field that is not publishable has no attribute.
- **Without the key, encrypted fields are absent**, not present and empty. The keyless
  pull keeps `CompletionDate`, `SubmissionDate`, `formdef_version`, `review_quality`,
  `review_status`, `KEY` and the publishable fields, and leaves out `starttime`,
  `endtime`, `deviceid`, `duration` and `instanceID`.
- The keyless pull adds one always-`""` column per group whose children are all
  encrypted (`meta` included), named after the group.
- The keyed and keyless pulls return the same `KEY`s, and the fields present in both
  are exactly the XML's publishable fields.
