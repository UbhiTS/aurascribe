import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
  componentStack: string | null;
}

/** Top-level React error boundary.
 *
 *  React will unmount the entire subtree on an uncaught render error —
 *  without a boundary, the webview shows a blank screen and the user has
 *  no recovery path. This component catches the error, renders a minimal
 *  fallback with the message + component stack, and exposes a "Reload"
 *  button. The underlying recording session continues in the sidecar
 *  regardless, so reloading is safe.
 *
 *  Error boundaries only work as class components — React doesn't expose a
 *  hook equivalent for `componentDidCatch` / `getDerivedStateFromError`.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, componentStack: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // console.error is picked up by `tauri dev`'s console and the webview
    // devtools. The full stack is also stored in state for the fallback UI.
    console.error("[ErrorBoundary] uncaught render error:", error, info);
    this.setState({ componentStack: info.componentStack ?? null });
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-950 text-gray-100 p-8">
        <div className="max-w-2xl w-full space-y-4">
          <div className="text-lg font-semibold text-red-400">
            Something went wrong in the UI.
          </div>
          <p className="text-sm text-gray-400 leading-relaxed">
            Your recording (if any) is safe — it's handled by the Python
            sidecar, not the UI. Reloading will recover.
          </p>
          <div className="rounded-lg border border-red-900/50 bg-red-950/20 p-3 text-xs font-mono text-red-200 whitespace-pre-wrap break-words">
            {this.state.error.message}
          </div>
          {this.state.componentStack && (
            <details className="text-xs text-gray-500">
              <summary className="cursor-pointer hover:text-gray-300">
                Component stack
              </summary>
              <pre className="mt-2 p-3 rounded-lg border border-gray-800 bg-gray-900/60 overflow-auto text-[10px] leading-snug whitespace-pre-wrap break-words">
                {this.state.componentStack}
              </pre>
            </details>
          )}
          <button
            onClick={() => window.location.reload()}
            className="px-4 py-2 text-sm rounded-lg bg-brand-600 hover:bg-brand-700 text-white transition-colors"
          >
            Reload
          </button>
        </div>
      </div>
    );
  }
}
