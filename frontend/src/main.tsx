import { AuthBoundary } from './components/AuthBoundary'
import { useCurrentUser } from './lib/auth'
import React from 'react'
import ReactDOM from 'react-dom/client'
import { RouterProvider } from 'react-router-dom'
import { QueryClient } from '@tanstack/react-query'
import { PersistQueryClientProvider } from '@tanstack/react-query-persist-client'
import { initializeFrontendExtensions } from './extensions/bootstrap'
import { createAppPersister, shouldPersistQuery, PERSIST_BUSTER } from './lib/queryPersist'
// 字体自托管 (@fontsource): 替代 rsms.me / Google Fonts 渲染阻塞外链,
// 内网/离线部署不再白屏等字体。权重覆盖 tailwind 全部用量 (300-900)。
import '@fontsource/inter/300.css'
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import '@fontsource/inter/700.css'
import '@fontsource/inter/900.css'
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/500.css'
import '@fontsource/jetbrains-mono/600.css'
import '@fontsource/jetbrains-mono/700.css'
import './index.css'

const BusinessApp = React.lazy(async () => {
  await initializeFrontendExtensions()
  const { router } = await import('./router')
  return { default: function Business() {
    const user = useCurrentUser()!
    const [queryClient] = React.useState(() => new QueryClient({ defaultOptions: { queries: { staleTime: 5_000, refetchOnWindowFocus: false } } }))
    const [persister] = React.useState(() => createAppPersister(user.id))
    React.useEffect(() => () => { void queryClient.cancelQueries(); queryClient.clear() }, [queryClient])
    return <PersistQueryClientProvider client={queryClient} persistOptions={{ persister, buster: PERSIST_BUSTER, maxAge: 24 * 60 * 60 * 1000, dehydrateOptions: { shouldDehydrateQuery: shouldPersistQuery } }}><RouterProvider router={router} /></PersistQueryClientProvider>
  } }
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><AuthBoundary><React.Suspense fallback={<div className="min-h-screen bg-base" />}><BusinessApp /></React.Suspense></AuthBoundary></React.StrictMode>,
)
