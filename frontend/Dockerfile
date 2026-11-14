# Build stage: produce the optimized static bundle.
FROM node:24-alpine AS build
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
# A STABLE nginx branch (an even minor), named by its branch, so a rebuild
# takes its patch releases and nothing else. A floating nginx:alpine is
# whatever image of that name the build host pulled last, and a host that
# never pulls again keeps building on it. A stable branch ends when the
# next is cut (about once a year): check nginx.org when you touch this.
FROM nginx:1.30-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
# The shared /api proxy directives, included by both API locations. Not a
# .conf, so nginx's conf.d glob never loads it on its own.
COPY api-proxy.inc /etc/nginx/conf.d/api-proxy.inc
# nginx:alpine runs everything in /docker-entrypoint.d/ before starting nginx.
COPY docker-inject-env.sh /docker-entrypoint.d/40-inject-env.sh
RUN chmod +x /docker-entrypoint.d/40-inject-env.sh
EXPOSE 5173
