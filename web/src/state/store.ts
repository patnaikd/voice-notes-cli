import { createContext, useContext, useRef, useSyncExternalStore } from "react";
import { initialState, reducer, type Action, type AppState } from "./reducer";

/**
 * A tiny external store around the single reducer. Components subscribe to
 * slices with `useAppState(selector)`, so a 5 Hz partial update re-renders
 * only the partial line, not the whole page.
 */
export interface Store {
  getState(): AppState;
  dispatch(action: Action): void;
  subscribe(listener: () => void): () => void;
}

export function createStore(initial: AppState = initialState): Store {
  let state = initial;
  const listeners = new Set<() => void>();
  return {
    getState: () => state,
    dispatch(action) {
      const next = reducer(state, action);
      if (next === state) return;
      state = next;
      for (const l of listeners) l();
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

export const StoreContext = createContext<Store | null>(null);

export function useStore(): Store {
  const store = useContext(StoreContext);
  if (!store) throw new Error("StoreContext missing");
  return store;
}

export function useAppState<T>(selector: (s: AppState) => T): T {
  const store = useStore();
  const sel = useRef(selector);
  sel.current = selector;
  return useSyncExternalStore(
    store.subscribe,
    () => sel.current(store.getState()),
    () => sel.current(store.getState()),
  );
}

export function useDispatch(): Store["dispatch"] {
  return useStore().dispatch;
}
