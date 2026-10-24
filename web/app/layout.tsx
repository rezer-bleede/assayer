import type { Metadata, Viewport } from 'next';
import { themeScript } from '@/lib/theme';
import '@openuidev/react-ui/styles/index.css';
import './globals.css';

export const metadata: Metadata = {
  title: {
    template: '%s | assayer',
    default: 'assayer — agentic testing & QA',
  },
  description:
    'Agentic testing and QA for data, ETL, web apps, and UI/UX. Connect a source or share a project, then let the agent build a focused test plan, run checks, and explain the results.',
  keywords: [
    'agentic testing',
    'QA automation',
    'software testing',
    'web app testing',
    'UI regression testing',
    'ETL testing',
    'data validation',
    'visual regression testing',
    'data pipeline testing',
    'schema validation',
  ].join(', '),
  authors: [{ name: 'assayer' }],
  publisher: 'assayer',
  category: 'Developer Tools',
  formatDetection: {
    telephone: false,
  },
  metadataBase: new URL('https://assayer.dev'),
  alternates: {
    canonical: '/',
  },
  openGraph: {
    title: 'assayer — agentic testing & QA',
    description:
      'Agentic testing and QA for data, ETL, web apps, and UI/UX. Connect a source or share a project, then let the agent build a focused test plan, run checks, and explain the results.',
    type: 'website',
    url: 'https://assayer.dev/',
    images: [
      {
        url: '/android-chrome-512x512.png',
        width: 512,
        height: 512,
        alt: 'assayer agentic testing and QA',
        type: 'image/png',
      },
    ],
    siteName: 'assayer',
    locale: 'en_US',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'assayer — agentic testing & QA',
    description:
      'Agentic testing and QA for data, ETL, web apps, and UI/UX. Connect a source or share a project, then let the agent build a focused test plan, run checks, and explain the results.',
    images: ['/android-chrome-512x512.png'],
  },
  robots: {
    index: true,
    follow: true,
    googleBot: {
      index: true,
      follow: true,
      'max-video-preview': -1,
      'max-image-preview': 'large',
      'max-snippet': -1,
    },
  },
};

export const viewport: Viewport = {
  // corrected before paint by the theme script, so an explicit choice is
  // honoured rather than the OS preference
  themeColor: '#fafafa',
  width: 'device-width',
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/*
          The theme has to be on <html> before the first paint, or the app
          flashes the wrong palette. This runs synchronously, ahead of the
          app's JavaScript — which is why it cannot be a React effect.
        */}
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
        <link rel="icon" href="/favicon.ico" sizes="any" />
        <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
        <link rel="icon" href="/favicon-32x32.png" sizes="32x32" type="image/png" />
        <link rel="icon" href="/favicon-16x16.png" sizes="16x16" type="image/png" />
        <link rel="apple-touch-icon" href="/apple-touch-icon.png" sizes="180x180" />
        <link rel="manifest" href="/site.webmanifest" />
        <link rel="sitemap" href="/sitemap.xml" />
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{
            __html: JSON.stringify({
              '@context': 'https://schema.org',
              '@type': 'SoftwareApplication',
              name: 'assayer',
              alternateName: 'assayer-agent',
              description:
                'Agentic testing and QA across data, ETL pipelines, web apps, and UI/UX. Build focused test plans, run checks, and review clear results.',
              applicationCategory: 'DeveloperApplication',
              operatingSystem: 'Linux, Windows, macOS',
              applicationSuite: 'assayer',
              offers: {
                '@type': 'Offer',
                price: '0',
                priceCurrency: 'USD',
                availability: 'https://schema.org/InStock',
                url: 'https://assayer.dev/',
              },
              author: {
                '@type': 'Organization',
                name: 'assayer',
              },
              publisher: {
                '@type': 'Organization',
                name: 'assayer',
                url: 'https://assayer.dev/',
              },
              keywords: [
                'agentic testing',
                'QA automation',
                'software testing',
                'web app testing',
                'UI regression testing',
                'ETL testing',
                'data validation',
                'visual regression testing',
                'data pipeline testing',
              ].join(','),
              features: [
                'Agentic testing and QA across data, ETL, web, and UI/UX',
                'SQL, Python, and ETL transformation analysis',
                'Automated test plan generation',
                'Human-in-the-loop approval',
                'Database and file validation',
                'Browser flows, UI checks, and visual regression testing',
                'Read-only query execution',
                'Row-level failure samples',
              ],
              softwareVersion: '0.1.0',
              datePublished: '2026-10-17',
              isAccessibleForFree: true,
            },
          )}}
        />
      </head>
      <body className="font-sans antialiased">{children}</body>
    </html>
  );
}
