import { cpSync, mkdirSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, '..');
const repoRoot = resolve(root, '..');

const pub = resolve(root, 'public');
mkdirSync(resolve(pub, 'screenshots'), { recursive: true });

// Logos
cpSync(resolve(repoRoot, 'src/frontend/public/logo-icon.svg'), resolve(pub, 'logo-icon.svg'));
cpSync(resolve(repoRoot, 'src/frontend/public/logo-icon.svg'), resolve(pub, 'favicon.svg'));
cpSync(resolve(repoRoot, 'src/frontend/public/logo.svg'), resolve(pub, 'logo.svg'));

// Docs assets
cpSync(resolve(repoRoot, 'docs/assets/architecture.svg'), resolve(pub, 'architecture.svg'));
cpSync(resolve(repoRoot, 'docs/assets/social-preview.svg'), resolve(pub, 'social-preview.svg'));

// Screenshots
const shots = [
  'chat-dark.png', 'chat-light.png',
  'knowledge-dark.png', 'knowledge-light.png',
  'integrations-dark.png', 'integrations-light.png',
  'memory-dark.png', 'memory-light.png',
  'rooms-dark.png', 'rooms-light.png',
  'satellites-dark.png', 'satellites-light.png',
];
for (const s of shots) {
  cpSync(resolve(repoRoot, 'docs/screenshots', s), resolve(pub, 'screenshots', s));
}

console.log('Assets copied successfully.');
