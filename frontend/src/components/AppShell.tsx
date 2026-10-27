import type { ReactNode } from 'react';
import { MessageCircle, Video, Inbox, History, Settings, ChevronDown, ListChecks, Bot, ClipboardCheck } from 'lucide-react';
import type { ConnectionStatus } from '../lib/types';
import { apiRequest } from '../lib/api';
import SimulationPanel from './SimulationPanel';

interface AppShellProps {
  currentRoute: string;
  onNavigate: (route: string) => void;
  session: ConnectionStatus | null;
  children: ReactNode;
  notice?: { kind: 'success' | 'info' | 'error'; message: string } | null;
  onDismissNotice?: () => void;
}

export default function AppShell({ currentRoute, onNavigate, session, children, notice, onDismissNotice }: AppShellProps) {
  const navItems = [
    { id: 'videos', label: 'Videos', icon: Video },
    { id: 'inbox', label: 'Comment inbox', icon: Inbox },
    { id: 'posted', label: 'Posted', icon: History },
    { id: 'jobs', label: 'Jobs', icon: ListChecks },
    { id: 'automation', label: 'Automation', icon: Bot },
    { id: 'review', label: 'Review queue', icon: ClipboardCheck },
    { id: 'settings', label: 'Settings', icon: Settings },
  ];

  return (
    <div className="min-h-screen flex flex-col">
      <div className="pt-4 px-6 max-w-[1440px] mx-auto w-full flex justify-center z-50">
        <header className="flex items-center justify-between gap-1 px-4 py-2 w-full rounded-2xl border border-dark-600 bg-dark-800/60 backdrop-blur-xl shadow-lg">
          <div className="flex items-center gap-2 text-xl font-bold cursor-pointer pr-4 max-[520px]:gap-1 max-[520px]:pr-2" onClick={() => onNavigate('videos')}>
            <MessageCircle className="text-brand w-6 h-6 fill-brand" />
            <span className="tracking-tight max-[520px]:hidden">Reply<span className="text-brand font-medium">AI</span></span>
          </div>
          
          <nav className="flex min-w-0 items-center gap-1 max-[520px]:gap-0">
            {navItems.map(item => {
              const Icon = item.icon;
              const isActive = currentRoute === item.id;
              return (
                <button
                  key={item.id}
                  onClick={() => onNavigate(item.id)}
                  aria-label={item.label}
                  className={`flex items-center gap-2 px-4 py-2 rounded-full text-sm font-medium transition-colors max-[520px]:gap-0 max-[520px]:px-2 ${
                    isActive ? 'bg-brand/10 text-brand' : 'text-text-muted hover:text-text-main hover:bg-dark-700/50'
                  }`}
                >
                  <Icon className={`w-4 h-4 ${isActive ? 'fill-brand/20' : ''}`} />
                  <span className="max-[520px]:hidden">{item.label}</span>
                </button>
              );
            })}
          </nav>

          <div className="flex items-center pl-4 max-[520px]:hidden">
            {session?.connected && session.channel ? (
              <div className="flex items-center gap-3 px-3 py-1.5 rounded-full hover:bg-dark-700/50 cursor-pointer transition-colors group">
                <div className="relative">
                  {session.channel.channel_thumbnail_url ? (
                    <img src={session.channel.channel_thumbnail_url} alt="" className="w-9 h-9 rounded-full border border-dark-600" />
                  ) : (
                    <div className="w-9 h-9 rounded-full bg-dark-600 border border-dark-600" />
                  )}
                </div>
                <div className="flex flex-col text-left">
                  <span className="text-sm font-medium leading-tight group-hover:text-white transition-colors">{session.channel.channel_title}</span>
                  <span className="text-[11px] text-success flex items-center gap-1.5 font-medium mt-0.5">
                    <span className="w-1.5 h-1.5 rounded-full bg-success inline-block shadow-[0_0_8px_rgba(16,185,129,0.5)]"></span> Creator
                  </span>
                </div>
                <ChevronDown className="w-4 h-4 text-text-muted ml-1 group-hover:text-white transition-colors" />
              </div>
            ) : (
              <div className="text-sm text-text-muted">Not connected</div>
            )}
          </div>
        </header>
      </div>
      
      {session?.simulation && (
        <div className="px-6 max-w-[1440px] mx-auto w-full">
          <div role="status" aria-label="Simulation mode"
            className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-warning/40 bg-warning/10 px-4 py-2 text-sm text-warning">
            <span><strong>SIMULATION MODE</strong> — simulated YouTube channel and scripted model responses.
              Nothing is sent to YouTube or to a model provider.</span>
            {!session.connected && (
              <button type="button" className="underline font-medium"
                onClick={() => void apiRequest('/api/simulation/connect', {
                  method: 'POST', headers: { 'X-CSRF-Token': session.csrf_token ?? '' },
                }).then(() => window.location.reload())}>
                Connect simulated channel
              </button>
            )}
            {session.connected && (
              <details className="w-full">
                <summary className="cursor-pointer font-medium">Simulation controls</summary>
                <SimulationPanel csrf={session.csrf_token ?? ''} />
              </details>
            )}
          </div>
        </div>
      )}

      {notice && (
        <div className="px-6 max-w-[1440px] mx-auto w-full">
          <div
            role="alert"
            className={`mt-3 flex items-start justify-between gap-3 rounded-xl border px-4 py-3 text-sm ${
              notice.kind === 'success'
                ? 'border-success/30 bg-success-bg text-success'
                : notice.kind === 'error'
                ? 'border-danger/30 bg-danger-bg text-danger'
                : 'border-dark-600 bg-dark-800/60 text-text-main'
            }`}
          >
            <span>{notice.message}</span>
            {onDismissNotice && (
              <button
                type="button"
                onClick={onDismissNotice}
                aria-label="Dismiss notice"
                className="shrink-0 underline"
              >
                Dismiss
              </button>
            )}
          </div>
        </div>
      )}

      <main className="flex-1 flex flex-col p-6 max-w-[1440px] mx-auto w-full">
        {children}
      </main>
    </div>
  );
}
