// WebSocket-обёртка с явной семантикой разрыва (см. DESIGN_BRIEF §11).
//
// КРИТИЧНО: разрыв здесь означает «процесс ApexCore упал/закрыт», не «нет
// интернета». UI не должен делать exponential backoff с retry-таймерами —
// надо показать overlay «ApexCore service is not running» и одну кнопку
// Retry. Эта обёртка экспонирует события 'open' / 'message' / 'down' и не
// делает auto-reconnect — это решает экран.

export class MetricsSocket extends EventTarget {
  constructor(path = '/ws/metrics') {
    super();
    this.ws = null;
    this.path = path;
    this.everConnected = false;
    this.lastConnectedAt = null;
    this.manualClose = false;
  }

  connect() {
    this.manualClose = false;
    const url = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}${this.path}`;
    try {
      this.ws = new WebSocket(url);
    } catch (err) {
      this.dispatchEvent(new CustomEvent('down', {
        detail: { reason: 'construct_failed', everConnected: this.everConnected, lastConnectedAt: this.lastConnectedAt, error: String(err) },
      }));
      return;
    }
    this.ws.onopen = () => {
      this.everConnected = true;
      this.lastConnectedAt = new Date();
      this.dispatchEvent(new CustomEvent('open'));
    };
    this.ws.onmessage = (event) => {
      try {
        const snap = JSON.parse(event.data);
        this.dispatchEvent(new CustomEvent('message', { detail: snap }));
      } catch (err) {
        // Битый JSON — игнорируем, не падаем. Один поломанный тик не должен ронять стрим.
        console.warn('MetricsSocket: bad JSON', err);
      }
    };
    this.ws.onerror = () => {
      // Ничего не делаем — onclose всё равно сработает.
    };
    this.ws.onclose = () => {
      if (this.manualClose) return;
      this.dispatchEvent(new CustomEvent('down', {
        detail: {
          reason: this.everConnected ? 'dropped' : 'cold-start',
          everConnected: this.everConnected,
          lastConnectedAt: this.lastConnectedAt,
        },
      }));
    };
  }

  close() {
    this.manualClose = true;
    if (this.ws) {
      try { this.ws.close(); } catch { /* ignore */ }
      this.ws = null;
    }
  }
}
