import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render as rtlRender, waitFor } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { http, HttpResponse } from 'msw';

import { EmbeddedCameraViewer } from '../../components/EmbeddedCameraViewer';
import { AuthProvider } from '../../contexts/AuthContext';
import { ThemeProvider } from '../../contexts/ThemeContext';
import { ToastProvider } from '../../contexts/ToastContext';
import i18n from '../../i18n';
import { server } from '../mocks/server';

vi.stubGlobal('navigator', {
  ...navigator,
  sendBeacon: vi.fn().mockReturnValue(true),
});

function renderEmbeddedCameraViewer() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });

  return rtlRender(
    <QueryClientProvider client={queryClient}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter>
          <AuthProvider>
            <ThemeProvider>
              <ToastProvider>
                <EmbeddedCameraViewer
                  printerId={2}
                  printerName="Creator 5 Pro"
                  onClose={() => {}}
                />
              </ToastProvider>
            </ThemeProvider>
          </AuthProvider>
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>
  );
}

describe('EmbeddedCameraViewer', () => {
  it('waits for a stream token before rendering the camera stream when auth is enabled', async () => {
    let resolveToken!: () => void;
    const tokenGate = new Promise<void>((resolve) => {
      resolveToken = resolve;
    });

    server.use(
      http.get('*/api/v1/auth/status', () =>
        HttpResponse.json({ auth_enabled: true, requires_setup: false })
      ),
      http.get('/api/v1/printers/:id', () =>
        HttpResponse.json({
          id: 2,
          name: 'Creator 5 Pro',
          ip_address: '192.0.2.211',
          serial_number: 'FF-TEST-SERIAL',
          access_code: 'ff-test-key',
          model: 'FlashForge Creator 5 Pro',
          enabled: true,
          camera_rotation: 0,
        })
      ),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json({
          connected: true,
          state: 'IDLE',
          progress: 0,
          capabilities: {
            can_chamber_light: false,
            can_skip_objects: false,
          },
        })
      ),
      http.get('/api/v1/printers/:id/camera/status', () =>
        HttpResponse.json({ active: false, stalled: false })
      ),
      http.post('/api/v1/printers/:id/camera/stop', () =>
        HttpResponse.json({ success: true })
      ),
      http.post('*/api/v1/printers/camera/stream-token', async () => {
        await tokenGate;
        return HttpResponse.json({ token: 'embedded-token' });
      })
    );

    renderEmbeddedCameraViewer();

    await waitFor(() => {
      expect(document.body.textContent).toContain('Creator 5 Pro');
    });
    expect(document.querySelector('img[alt="Camera stream"]')).toBeNull();

    resolveToken();

    await waitFor(() => {
      const img = document.querySelector('img[alt="Camera stream"]') as HTMLImageElement | null;
      expect(img).not.toBeNull();
      expect(img?.getAttribute('src')).toContain('/api/v1/printers/2/camera/stream');
      expect(img?.getAttribute('src')).toContain('token=embedded-token');
    });
  });
});
