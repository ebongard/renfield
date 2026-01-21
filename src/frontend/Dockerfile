FROM node:20-alpine

WORKDIR /app

# Package files kopieren
COPY package*.json ./

# Dependencies installieren
RUN npm install

# App Code kopieren
COPY . .

# Port exposieren
EXPOSE 3000

# Development Server starten
CMD ["npm", "run", "dev", "--", "--host"]
