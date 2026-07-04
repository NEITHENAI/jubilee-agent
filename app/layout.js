import './globals.css'

export const metadata = {
  title: 'Jubilee AI Agent',
  description: 'AI assistant powered by DeepSeek and Jubilee Knowledge Base.',
}

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <head>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet" />
      </head>
      <body>{children}</body>
    </html>
  )
}
