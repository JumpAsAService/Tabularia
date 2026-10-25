FROM node:22-alpine

WORKDIR /app

# runs as the 'node' user (uid 1000): it matches the host user, so the files
# generated in the bind mount (.nuxt, etc.) are NOT owned by root on the host
RUN chown node:node /app
USER node

# installs the dependencies (cacheable layer). npm ci from the lockfile: a
# deterministic, reproducible build — it also avoids npm's arborist crash
# (`Cannot read properties of null (reading 'edgesOut')`) on a fresh install
# without a lockfile. The lockfile must be kept in sync with package.json.
COPY --chown=node:node package.json package-lock.json ./
RUN npm ci

COPY --chown=node:node . .

ENV NUXT_PUBLIC_API_BASE=http://localhost:8000 \
    NUXT_PUBLIC_BUCKET=data-prep

EXPOSE 3000

# dev server with hot reload; --host to expose it outside the container
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
