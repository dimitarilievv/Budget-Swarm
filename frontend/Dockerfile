# syntax=docker/dockerfile:1
#
# BudgetSwarm frontend image (BS-103). Multi-stage build:
#   deps         install node_modules from the lockfile (cached layer)
#   development  Vite dev server + HMR; source is bind-mounted by compose
#   build        type-check + production bundle (vite build -> /app/dist)
#   production   static bundle served by nginx with SPA fallback
#
# Dev compose builds `--target development` and bind-mounts ./frontend over /app,
# with an anonymous volume preserving the image's /app/node_modules.

FROM node:20-alpine AS deps
WORKDIR /app
# Copy manifests first so the install layer is cached across source changes.
# `npm ci` installs exactly what package-lock.json pins (reproducible builds).
COPY package.json package-lock.json ./
RUN npm ci

# --- development ------------------------------------------------------------
# Source is bind-mounted by compose; the COPY only seeds a standalone image.
FROM deps AS development
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev"]

# --- build ------------------------------------------------------------------
FROM deps AS build
# Empty default => the app uses a relative, same-origin API path (/api) behind a
# proxy. Override for a different API origin: --build-arg VITE_API_BASE_URL=...
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
COPY . .
# `npm run build` runs `tsc --noEmit && vite build`, so a type error fails the build.
RUN npm run build

# --- production -------------------------------------------------------------
FROM nginx:1.27-alpine AS production
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -q --spider http://localhost:80/ || exit 1
# nginx:alpine's default entrypoint/CMD runs nginx in the foreground.
