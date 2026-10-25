-- Secondary indexes created AFTER the bulk load (COPY into tables without indexes
-- is much faster; here they are built in one go). They serve the typical joins of
-- the margin calculation and the attribution by customer/agent.

CREATE INDEX IF NOT EXISTS ix_listino_prodotto     ON catalogo.listino_prezzi (prodotto_id);
CREATE INDEX IF NOT EXISTS ix_costiprod_prodotto    ON catalogo.costi_produzione (prodotto_id);

CREATE INDEX IF NOT EXISTS ix_ordini_cliente        ON vendite.ordini (cliente_id);
CREATE INDEX IF NOT EXISTS ix_ordini_agente         ON vendite.ordini (agente_id);
CREATE INDEX IF NOT EXISTS ix_ordini_data           ON vendite.ordini (data_ordine);

CREATE INDEX IF NOT EXISTS ix_righe_ordine          ON vendite.righe_ordine (ordine_id);
CREATE INDEX IF NOT EXISTS ix_righe_prodotto        ON vendite.righe_ordine (prodotto_id);

CREATE INDEX IF NOT EXISTS ix_costicomm_riga        ON vendite.costi_commerciali (riga_ordine_id);

ANALYZE;
