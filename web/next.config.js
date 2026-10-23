/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  reactStrictMode: true,
  transpilePackages: ['@openuidev/react-lang', '@openuidev/react-ui', '@openuidev/react-headless'],
  // Stack traces in the browser error boundary point at real source.
  // Off by default: source maps ship the original source to the client.
  productionBrowserSourceMaps: process.env.NEXT_SOURCE_MAPS === 'true',
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8010',
    NEXT_PUBLIC_WS_URL: process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:8010',
  },
};

module.exports = nextConfig;