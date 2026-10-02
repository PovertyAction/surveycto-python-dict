# surveycto-python-dict

Pulls a SurveyCTO form's compiled XML and wide JSON over the API and builds, from them,
`questions.json`, sections, typed parquet, a variable dictionary and graph, and a
per-variable PII signal.
Design: `surveycto-api-extractor-PROMPT.md`.

Credentials come from `.env` (`SCTO_SERVER`, `SCTO_USER`, `SCTO_PASSWORD`, `SCTO_KEY_PATH`);
never commit it or the `.pem` key.
