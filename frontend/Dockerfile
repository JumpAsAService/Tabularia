FROM node:22-alpine

WORKDIR /app

# gira come utente 'node' (uid 1000): combacia con l'utente host, così i file
# generati nel bind-mount (.nuxt, ecc.) NON risultano di root sull'host
RUN chown node:node /app
USER node

# installa le dipendenze (layer cache-abile)
COPY --chown=node:node package.json ./
RUN npm install

COPY --chown=node:node . .

ENV NUXT_PUBLIC_API_BASE=http://localhost:8000 \
    NUXT_PUBLIC_BUCKET=data-prep

EXPOSE 3000

# dev server con hot-reload; --host per esporlo fuori dal container
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
