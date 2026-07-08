# Tabularia — Gateway (control plane)

Servizio pubblico che fa da unico ingresso all'applicazione. Possiede:

- **Postgres** (metadati: utenti, gruppi, progetti/cartelle, permessi)
- **Auth** JWT (login, `current_user`)
- **RBAC** stile Tableau: permessi su progetti, ereditati lungo l'albero delle cartelle
- **Proxy** verso l'**engine** interno (Celery + Polars), che non è esposto pubblicamente

```
browser → gateway (:8000, pubblico) → engine (interno, Celery+Polars) → worker → MinIO
```

L'engine (`../backend`) resta ignaro di utenti e permessi: tutta l'identità vive qui.
