FROM python:3.12-slim

# openssl CLI is a hard dependency (PKCS#7 degenerate packaging + CSR
# parsing) -- ldap3 is pure Python (no libldap/libsasl needed).
RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY est_shim.py .

# Required env vars (BAO_ROLE_ID, BAO_SECRET_ID) have no defaults --
# provide them via `docker run -e`, `--env-file`, or your orchestrator's
# secret mechanism. See est-shim.env.example.
EXPOSE 8085
CMD ["python3", "est_shim.py"]
