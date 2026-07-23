FROM node:22-alpine

WORKDIR /app

# gira come utente 'node' (uid 1000): combacia con l'utente host, così i file
# generati nel bind-mount (.nuxt, ecc.) NON risultano di root sull'host
RUN chown node:node /app
USER node

# installa le dipendenze (layer cache-abile). npm ci dal lockfile: build
# deterministico e riproducibile — evita anche il crash dell'arborist di npm
# (`Cannot read properties of null (reading 'edgesOut')`) sul fresh install
# senza lockfile. Il lockfile va tenuto in sync con package.json.
COPY --chown=node:node package.json package-lock.json ./
RUN npm ci

COPY --chown=node:node . .

ENV NUXT_PUBLIC_API_BASE=http://localhost:8000 \
    NUXT_PUBLIC_BUCKET=data-prep

EXPOSE 3000

# dev server con hot-reload; --host per esporlo fuori dal container
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
