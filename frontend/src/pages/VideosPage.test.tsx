import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import VideosPage from './VideosPage';
import type { VideoItem } from '../lib/types';

const videos: VideoItem[] = [
  {
    video_id: 'video-1',
    title: 'Build an AI app with Python',
    thumbnail_url: null,
    published_at: '2026-09-28T12:00:00Z',
    watch_url: 'https://youtube.com/watch?v=video-1',
  },
  {
    video_id: 'video-2',
    title: 'Deploy Your AI App to Vercel',
    thumbnail_url: null,
    published_at: null,
    watch_url: 'https://youtube.com/watch?v=video-2',
  },
  {
    video_id: 'video-3',
    title: 'Fine-Tune LLMs',
    thumbnail_url: null,
    published_at: null,
    watch_url: 'https://youtube.com/watch?v=video-3',
  },
];

const commentsByVideo = {
  'video-1': [
    {
      comment_id: 'comment-1',
      author_name: 'Ananya',
      author_thumbnail_url: null,
      published_at: null,
      text: 'Can you share the GitHub repo for this project?',
      total_reply_count: 0,
      reply_status: 'Needs review',
      video_id: 'video-1',
      video_title: videos[0].title,
    },
    {
      comment_id: 'comment-2',
      author_name: 'Daniel',
      author_thumbnail_url: null,
      published_at: null,
      text: 'This was really helpful. Thank you!',
      total_reply_count: 1,
      reply_status: 'Posted',
      video_id: 'video-1',
      video_title: videos[0].title,
    },
  ],
  'video-2': [
    {
      comment_id: 'comment-3',
      author_name: 'Mina',
      author_thumbnail_url: null,
      published_at: null,
      text: 'Which deployment option did you use?',
      total_reply_count: 0,
      reply_status: 'Draft ready',
      video_id: 'video-2',
      video_title: videos[1].title,
    },
  ],
  'video-3': [],
};

afterEach(cleanup);

function VideosHarness() {
  const [selectedVideo, setSelectedVideo] = useState<VideoItem | null>(videos[0]);
  const [selectedComment, setSelectedComment] = useState<string | null>(null);
  const comments = selectedVideo
    ? commentsByVideo[selectedVideo.video_id as keyof typeof commentsByVideo]
    : [];

  return (
    <VideosPage
      appData={{
        session: {
          csrf_token: '',
          oauth_configured: true,
          setup_message: null,
          connected: true,
          channel: null,
        },
        videos,
        videosLoading: false,
        videosError: null,
        comments,
        commentsLoading: false,
        commentsError: null,
        selectedVideo,
        selectVideo: (video) => {
          setSelectedVideo(video);
          setSelectedComment(null);
        },
        selectedComment,
        setSelectedComment,
        loadVideoPage: vi.fn(),
      }}
    />
  );
}

describe('VideosPage carousel', () => {
  it('renders the 3D carousel with the selected video and accessible controls', () => {
    render(<VideosHarness />);

    const carousel = screen.getByRole('region', { name: 'Your videos' });
    expect(carousel).toHaveAttribute('tabindex', '0');
    expect(screen.getByRole('button', { name: 'Select video: Build an AI app with Python' }))
      .toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Select video: Deploy Your AI App to Vercel' }))
      .toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByRole('button', { name: 'Previous video' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Next video' })).toBeEnabled();
  });

  it('selects a video by clicking its card and updates its comment context', () => {
    render(<VideosHarness />);

    fireEvent.click(screen.getByRole('button', { name: 'Select video: Deploy Your AI App to Vercel' }));

    expect(screen.getByRole('button', { name: 'Select video: Deploy Your AI App to Vercel' }))
      .toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('Which deployment option did you use?')).toBeInTheDocument();
    expect(screen.queryByText('Can you share the GitHub repo for this project?')).not.toBeInTheDocument();
    expect(screen.getByText(/Select a comment to open its saved reply/)).toBeInTheDocument();
  });

  it('moves between selected videos with the on-screen controls', () => {
    render(<VideosHarness />);

    fireEvent.click(screen.getByRole('button', { name: 'Next video' }));
    expect(screen.getByRole('button', { name: 'Select video: Deploy Your AI App to Vercel' }))
      .toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(screen.getByRole('button', { name: 'Previous video' }));
    expect(screen.getByRole('button', { name: 'Select video: Build an AI app with Python' }))
      .toHaveAttribute('aria-pressed', 'true');
  });

  it('navigates with ArrowLeft, ArrowRight, Home, and End while the carousel is focused', () => {
    render(<VideosHarness />);
    const carousel = screen.getByRole('region', { name: 'Your videos' });
    carousel.focus();

    fireEvent.keyDown(carousel, { key: 'ArrowRight' });
    expect(screen.getByRole('button', { name: 'Select video: Deploy Your AI App to Vercel' }))
      .toHaveAttribute('aria-pressed', 'true');

    fireEvent.keyDown(carousel, { key: 'ArrowLeft' });
    expect(screen.getByRole('button', { name: 'Select video: Build an AI app with Python' }))
      .toHaveAttribute('aria-pressed', 'true');

    fireEvent.keyDown(carousel, { key: 'End' });
    expect(screen.getByRole('button', { name: 'Select video: Fine-Tune LLMs' }))
      .toHaveAttribute('aria-pressed', 'true');

    fireEvent.keyDown(carousel, { key: 'Home' });
    expect(screen.getByRole('button', { name: 'Select video: Build an AI app with Python' }))
      .toHaveAttribute('aria-pressed', 'true');
  });

  it('selects a focused video card with Enter and Space', async () => {
    const user = userEvent.setup();
    render(<VideosHarness />);
    const secondVideo = screen.getByRole('button', { name: 'Select video: Deploy Your AI App to Vercel' });
    secondVideo.focus();

    await user.keyboard('{Enter}');
    expect(screen.getByRole('button', { name: 'Select video: Deploy Your AI App to Vercel' }))
      .toHaveAttribute('aria-pressed', 'true');

    const thirdVideo = screen.getByRole('button', { name: 'Select video: Fine-Tune LLMs' });
    thirdVideo.focus();
    await user.keyboard(' ');
    expect(screen.getByRole('button', { name: 'Select video: Fine-Tune LLMs' }))
      .toHaveAttribute('aria-pressed', 'true');
  });

  it('does not intercept arrow keys from an input inside the carousel region', () => {
    const selectVideo = vi.fn();
    render(
      <VideosPage
        appData={{
          session: null,
          videos,
          videosLoading: false,
          videosError: null,
          comments: [],
          commentsLoading: false,
          commentsError: null,
          selectedVideo: videos[0],
          selectVideo,
          selectedComment: null,
          setSelectedComment: vi.fn(),
          loadVideoPage: vi.fn(),
        }}
      />,
    );
    const carousel = screen.getByRole('region', { name: 'Your videos' });
    const input = document.createElement('input');
    carousel.append(input);

    fireEvent.keyDown(input, { key: 'ArrowRight' });

    expect(selectVideo).not.toHaveBeenCalled();
  });

  it('filters comments by their actual reply status', () => {
    render(<VideosHarness />);
    const commentFilters = within(screen.getByRole('group', { name: 'Filter comments' }));

    fireEvent.click(commentFilters.getByRole('button', { name: /Needs review/ }));
    expect(screen.getByText('Can you share the GitHub repo for this project?')).toBeInTheDocument();
    expect(screen.queryByText('This was really helpful. Thank you!')).not.toBeInTheDocument();

    fireEvent.click(commentFilters.getByRole('button', { name: /Drafts/ }));
    expect(screen.getByText('No comments match this filter.')).toBeInTheDocument();
  });
});
