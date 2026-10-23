import type { Metadata, Viewport } from 'next';
import { themeScript } from '@/lib/theme';
import '@openuidev/react-ui/styles/index.css';
import './globals.css';

export const metadata: Metadata = {
  title: {
    template: '%s | qualitest',
    default: 'qualitest — agentic data quality auditing',
  },
  description:
    'Agentic data testing and data quality auditing tool. Paste your data connection URLs and ETL scripts — the agent figures out transformations, designs a test plan, runs it against live sources, and reports failures with row-level samples.',
  keywords: [
    'data quality',
    'data testing',
    'data quality auditing',
    'ETL testing',
    'data quality agent',
    'data pipeline testing',
    'data observability',
    'schema validation',
    'data integrity',
  ].join(', '),
  authors: [{ name: 'qualitest' }],
  publisher: 'qualitest',
  category: 'Developer Tools',
  formatDetection: {
    telephone: false,
  },
  metadataBase: new URL('https://qualitest.dev'),
  alternates: {
    canonical: '/',
  },
  openGraph: {
    title: 'qualitest — agentic data quality auditing',
    description:
      'Agentic data testing and data quality auditing tool. Paste your data connection URLs and ETL scripts — the agent figures out transformations, designs a test plan, runs it against live sources, and reports failures with row-level samples.',
    type: 'website',
    url: 'https://qualitest.dev/',
    images: [
      {
        url: '/android-chrome-512x512.png',
        width: 512,
        height: 512,
        alt: 'qualitest logo',
        type: 'image/png',
      },
    ],
    siteName: 'qualitest',
    locale: 'en_US',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'qualitest — agentic data quality auditing',
    description:
      'Agentic data testing and data quality auditing tool. Paste your data connection URLs and ETL scripts — the agent figures out transformations, designs a test plan, runs it against live sources, and reports failures with row-level samples.',
    images: ['/android-chrome-512x512.png'],
    creator: '@qualitest',
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
              name: 'qualitest',
              alternateName: 'qualitest-agent',
              description:
                'Agentic data testing and data quality auditing tool for ETL pipelines, databases, and data platforms.',
              applicationCategory: 'DeveloperApplication',
              operatingSystem: 'Linux, Windows, macOS',
              applicationSuite: 'qualitest-agent',
              offers: {
                '@type': 'Offer',
                price: '0',
                priceCurrency: 'USD',
                availability: 'https://schema.org/InStock',
                url: 'https://qualitest.dev/',
              },
              author: {
                '@type': 'Organization',
                name: 'qualitest',
              },
              publisher: {
                '@type': 'Organization',
                name: 'qualitest',
                url: 'https://qualitest.dev/',
              },
              keywords: [
                'data quality',
                'data testing',
                'data quality auditing',
                'ETL testing',
                'agentic data quality',
                'data pipeline testing',
                'data observability',
              ].join(','),
              features: [
                'Agentic data testing framework',
                'SQL, Python, and dbt ETL parsing',
                'Automated test plan generation',
                'Human-in-the-loop approval',
                'Multi-database support (PostgreSQL, MySQL, ClickHouse, Snowflake, BigQuery, Databricks, S3)',
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
