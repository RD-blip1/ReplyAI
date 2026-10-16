import { useState, useEffect, useRef, useCallback } from 'react';
import AppShell from './components/AppShell';
import { useAppData } from './hooks/useAppData';
import VideosPage from './pages/VideosPage';
import InboxPage from './pages/InboxPage';
import PostedPage from './pages/PostedPage';
import SettingsPage from './pages/SettingsPage';

type Notice = { kind: 'success' | 'info' | 'error'; message: string }

// The backend returns a distinct ?oauth= result on the callback redirect, but nothing
// consumed it before, so a denied or failed connection looked identical to doing nothing.
const OAUTH_NOTICES: Record<string, Notice> = {
  connected: { kind: 'success', message: 'YouTube channel connected.' },
  permissions_updated: { kind: 'success', message: 'YouTube permissions updated.' },
  denied: { kind: 'info', message: 'Google sign-in was cancelled or denied. No channel was connected.' },
  permission_denied: { kind: 'info', message: 'You declined the extra YouTube permission. Your existing connection is unchanged.' },
  scope_required: { kind: 'info', message: 'The required YouTube permission was not granted, so comments and posting stay unavailable.' },
  no_channel: { kind: 'error', message: 'No YouTube channel was found for that Google account.' },
  channel_mismatch: { kind: 'error', message: 'That Google account does not own the connected channel.' },
  invalid_state: { kind: 'error', message: 'That sign-in attempt expired or was already used. Start the connection again.' },
  setup_required: { kind: 'error', message: 'Google OAuth is not configured on the backend yet.' },
  failed: { kind: 'error', message: 'The YouTube connection could not be completed. Try again.' },
}

function readOAuthNotice(): Notice | null {
  const result = new URLSearchParams(window.location.search).get('oauth')
  if (!result) return null
  return OAUTH_NOTICES[result] ?? {
    kind: 'error', message: 'The YouTube connection returned an unexpected result.' }
}

export default function App() {
  const [currentRoute, setCurrentRoute] = useState(window.location.hash.slice(1) || 'videos');
  const currentRouteRef = useRef(currentRoute)
  const unsavedPreferences = useRef(false)
  const [oauthNotice, setOauthNotice] = useState<Notice | null>(readOAuthNotice)
  const appData = useAppData();
  const onSettingsDirtyChange = useCallback((value: boolean) => {
    unsavedPreferences.current = value
  }, [])

  useEffect(() => {
    const onHashChange = () => {
      const nextRoute = window.location.hash.slice(1) || 'videos'
      if (currentRouteRef.current === 'settings' && nextRoute !== 'settings' && unsavedPreferences.current) {
        if (!window.confirm('Discard unsaved reply preference changes?')) {
          window.location.hash = currentRouteRef.current
          return
        }
        unsavedPreferences.current = false
      }
      currentRouteRef.current = nextRoute
      setCurrentRoute(nextRoute);
    }
    window.addEventListener('hashchange', onHashChange);
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!unsavedPreferences.current) return
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => {
      window.removeEventListener('hashchange', onHashChange)
      window.removeEventListener('beforeunload', onBeforeUnload)
    }
  }, []);

  const handleNavigate = (route: string) => {
    if (currentRouteRef.current === 'settings' && route !== 'settings' && unsavedPreferences.current) {
      if (!window.confirm('Discard unsaved reply preference changes?')) return
      unsavedPreferences.current = false
    }
    window.location.hash = route;
    currentRouteRef.current = route
    setCurrentRoute(route);
  };

  // Strip a one-time OAuth callback result from the URL so a refresh does not repeat the
  // message. The value itself was already read during the first render above.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    if (!params.has('oauth')) return
    params.delete('oauth')
    const query = params.toString()
    window.history.replaceState(
      {}, '', `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`)
  }, [])

  // When the backend has no OAuth client configured, surface its guidance instead of
  // letting the Connect button appear to do nothing.
  const setupNotice: Notice | null = appData.session && !appData.session.connected
    && !appData.session.oauth_configured
    ? { kind: 'info', message: appData.session.setup_message
        || 'Google OAuth is not configured yet. See docs/FRESH_CLONE_SETUP.md.' }
    : null
  const notice = oauthNotice ?? setupNotice

  return (
    <AppShell
      currentRoute={currentRoute}
      onNavigate={handleNavigate}
      session={appData.session}
      notice={notice}
      onDismissNotice={oauthNotice ? () => setOauthNotice(null) : undefined}
    >
      {currentRoute === 'videos' && <VideosPage appData={appData} />}
      {currentRoute === 'inbox' && <InboxPage appData={appData} />}
      {currentRoute === 'posted' && <PostedPage appData={appData} />}
      {currentRoute === 'settings' && (
        <SettingsPage
          key={appData.session?.channel?.channel_id ?? 'disconnected'}
          appData={appData}
          onDirtyChange={onSettingsDirtyChange}
        />
      )}
    </AppShell>
  );
}
