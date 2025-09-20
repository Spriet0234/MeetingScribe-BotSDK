def chunk_text(text: str, max_chars=16000, overlap=800):
    chunks = []
    i = 0
    L = len(text)
    while i < L:
        j = min(L, i + max_chars)
        k = text.rfind("\n", i + max_chars//2, j)
        if k == -1: k = j
        chunks.append(text[i:k])
        i = max(k - overlap, k)
    return [c for c in chunks if c.strip()]
