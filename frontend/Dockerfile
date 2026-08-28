# Build stage: produce the optimized static bundle.
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
# Bake unique placeholders for the runtime-config values; the entrypoint below
# replaces them with the real env at container start. Vite inlines VITE_* at
# BUILD time, so this is how one built image stays configurable per instance
# instead of needing a rebuild per operator.
#
# The defaults that actually apply in a running container are the shell
# defaults in docker-inject-env.sh, NOT the `|| 'Architecture Zero'` fallbacks
# in the TSX: esbuild constant-folds those away, because the placeholder it
# sees at build time is a truthy string. Change one, change the other.
ENV VITE_API_URL=__RT_VITE_API_URL__ \
    VITE_INSTANCE_NAME=__RT_VITE_INSTANCE_NAME__ \
    VITE_PRIMARY_COLOR=__RT_VITE_PRIMARY_COLOR__
RUN npm run build

# Runtime stage: serve the static bundle with nginx (no dev server).
FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
# nginx:alpine runs everything in /docker-entrypoint.d/ before starting nginx.
COPY docker-inject-env.sh /docker-entrypoint.d/40-inject-env.sh
RUN chmod +x /docker-entrypoint.d/40-inject-env.sh
EXPOSE 5173
