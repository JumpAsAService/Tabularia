-- Indici secondari creati DOPO il caricamento bulk (COPY su tabelle senza indici
-- è molto più veloce; qui si costruiscono in blocco). Servono ai join tipici del
-- calcolo del margine e all'attribuzione per cliente/agente.

CREATE INDEX IF NOT EXISTS ix_listino_prodotto     ON catalogo.listino_prezzi (prodotto_id);
CREATE INDEX IF NOT EXISTS ix_costiprod_prodotto    ON catalogo.costi_produzione (prodotto_id);

CREATE INDEX IF NOT EXISTS ix_ordini_cliente        ON vendite.ordini (cliente_id);
CREATE INDEX IF NOT EXISTS ix_ordini_agente         ON vendite.ordini (agente_id);
CREATE INDEX IF NOT EXISTS ix_ordini_data           ON vendite.ordini (data_ordine);

CREATE INDEX IF NOT EXISTS ix_righe_ordine          ON vendite.righe_ordine (ordine_id);
CREATE INDEX IF NOT EXISTS ix_righe_prodotto        ON vendite.righe_ordine (prodotto_id);

CREATE INDEX IF NOT EXISTS ix_costicomm_riga        ON vendite.costi_commerciali (riga_ordine_id);

ANALYZE;
