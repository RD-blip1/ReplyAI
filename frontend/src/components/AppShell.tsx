import { useState, type ReactNode } from 'react';
import { MessageCircle, Video, Inbox, History, Settings, ChevronDown, ChevronRight, ListChecks, Bot, ClipboardCheck, X } from 'lucide-react';
import type { ConnectionStatus } from '../lib/types';
import { apiRequest } from '../lib/api';
import SimulationPanel from './SimulationPanel';
import { Avatar } from './ui';
import './AppShell.css';

interface AppShellProps {
  currentRoute: string;
  onNavigate: (route: string) => void;
  session: ConnectionStatus | null;
  children: ReactNode;
  notice?: { kind: 'success' | 'info' | 'error'; message: string } | null;
  onDismissNotice?: () => void;
}

const NAV_ITEMS = [
  { id: 'videos', label: 'Videos', icon: Video },
  { id: 'inbox', label: 'Comment inbox', icon: Inbox },
  { id: 'posted', label: 'Posted', icon: History },
  { id: 'jobs', label: 'Jobs', icon: ListChecks },
  { id: 'automation', label: 'Automation', icon: Bot },
  { id: 'review', label: 'Review queue', icon: ClipboardCheck },
  { id: 'settings', label: 'Settings', icon: Settings },
];

export default function AppShell({ currentRoute, onNavigate, session, children, notice, onDismissNotice }: AppShellProps) {
  const [simulationOpen, setSimulationOpen] = useState(false);
  const channel = session?.connected ? session.channel : null;

  return (
    <div className="shell">
      <div className="shell-frame">
        <header className="shell-nav">
          <button type="button" className="shell-brand" onClick={() => onNavigate('videos')} aria-label="ReplyAI home">
            <span className="shell-logo" aria-hidden="true"><MessageCircle /></span>
            <span className="shell-wordmark">Reply<span>AI</span></span>
          </button>

          <nav className="shell-links" aria-label="Main">
            {NAV_ITEMS.map(item => {
              const Icon = item.icon;
              const isActive = currentRoute === item.id;
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => onNavigate(item.id)}
                  aria-label={item.label}
                  aria-current={isActive ? 'page' : undefined}
                  title={item.label}
                  className={`shell-link ${isActive ? 'is-active' : ''}`}
                >
                  <Icon aria-hidden="true" />
                  <span className="shell-link-label">{item.label}</span>
                </button>
              );
            })}
          </nav>

          {channel ? (
            <button type="button" className="shell-channel" onClick={() => onNavigate('settings')}
              title={`${channel.channel_title} · open settings`}>
              <Avatar name={channel.channel_title || 'Channel'} src={channel.channel_thumbnail_url} size={36} color="var(--accent)" />
              <span className="shell-channel-text">
                <span className="shell-channel-name">{channel.channel_title}</span>
                <span className="shell-channel-role"><span className="shell-dot" aria-hidden="true" />Creator</span>
              </span>
              <ChevronDown className="shell-channel-chevron" aria-hidden="true" />
            </button>
          ) : (
            <span className="shell-channel shell-channel-empty">Not connected</span>
          )}
        </header>

        {session?.simulation && (
          <div role="status" aria-label="Simulation mode" className="shell-banner">
            <div className="shell-banner-row">
              <p className="min-w-0">
                <strong>Simulation mode</strong> — simulated YouTube channel and scripted model responses.
                Nothing is sent to YouTube or to a model provider.
              </p>
              {!session.connected ? (
                <button type="button" className="btn btn-sm"
                  onClick={() => void apiRequest('/api/simulation/connect', {
                    method: 'POST', headers: { 'X-CSRF-Token': session.csrf_token ?? '' },
                  }).then(() => window.location.reload())}>
                  Connect simulated channel
                </button>
              ) : (
                <button type="button" className="btn btn-sm shell-banner-toggle" aria-expanded={simulationOpen}
                  onClick={() => setSimulationOpen(open => !open)}>
                  <ChevronRight className={simulationOpen ? 'rotate-90' : ''} aria-hidden="true" />
                  Simulation controls
                </button>
              )}
            </div>
            {session.connected && simulationOpen && (
              <div className="shell-banner-panel">
                <SimulationPanel csrf={session.csrf_token ?? ''} />
              </div>
            )}
          </div>
        )}

        {notice && (
          <div
            role="alert"
            className={`alert justify-between ${
              notice.kind === 'success' ? 'alert-success' : notice.kind === 'error' ? 'alert-error' : ''
            }`}
          >
            <span className="min-w-0">{notice.message}</span>
            {onDismissNotice && (
              <button type="button" onClick={onDismissNotice} aria-label="Dismiss notice"
                className="btn btn-ghost btn-sm btn-icon -my-1.5">
                <X aria-hidden="true" />
              </button>
            )}
          </div>
        )}

        <main className="shell-main">
          {children}
        </main>
      </div>
    </div>
  );
}
