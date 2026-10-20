SELECT doc_id
FROM IDENTIFIER({{catalog}} || '.' || {{schema}} || '.documents')
WHERE status IN ('REGISTERED')
ORDER BY registered_at, doc_id
LIMIT LEAST(CAST({{max_docs}} AS INT), 500);
