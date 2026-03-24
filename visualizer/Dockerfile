FROM node:20-alpine

LABEL description="Dashboard de ranking en vivo para el Miner de Palabras de GitHub (Node.js)"

WORKDIR /app

# Instalar dependencias primero (aprovecha la caché de capas)
COPY package.json .
RUN npm install --omit=dev

# Copiar servidor y archivos estáticos
COPY server.js .
COPY public/ public/

EXPOSE 5000

CMD ["node", "server.js"]