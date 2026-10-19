'use client';

/**
 * Light/dark theme, resolved before first paint and remembered.
 *
 * The palette lives in CSS variables (app/globals.css) keyed off a class on
 * <html>, so the whole app is themed by one attribute. The class is set by a
 * tiny inline script in layout.tsx *before* the browser paints — a React
 * effect would run after the first frame and flash the wrong theme.
 *
 * A stored choice always wins; without one, the OS preference decides.
 */

import { useCallback, useEffect, useState } from 'react';
import { storage } from './storage';

export type Theme = 'light' | 'dark';
export type ThemeChoice = Theme | 'system';

const STORAGE_KEY = 'qualitest.theme';
const DARK_QUERY = '(prefers-color-scheme: dark)';

/** Browser chrome colours, matching the CSS palette. */
const CHROME = { light: '#fafafa', dark: '#0a0a0b' };

/** The theme actually in effect right now (never 'system'). */
export function resolvedTheme(choice: ThemeChoice): Theme {
  if (choice !== 'system') return choice;
  if (typeof window === 'undefined' || !window.matchMedia) return 'light';
  return window.matchMedia(DARK_QUERY).matches ? 'dark' : 'light';
}

/**
 * Paint the theme onto <html>. Also mirrors it into `data-theme`, so CSS can
 * ask "what is the user asking for" without caring whether that came from a
 * class or the OS.
 */
export function applyTheme(choice: ThemeChoice): Theme {
  const theme = resolvedTheme(choice);
  if (typeof document === 'undefined') return theme;
  const root = document.documentElement;
  root.classList.remove('light', 'dark');
  root.classList.add(theme);
  root.dataset.theme = choice;
  // the browser chrome follows the app, not the OS
  document
    .querySelectorAll('meta[name="theme-color"]')
    .forEach((meta) => meta.setAttribute('content', CHROME[theme]));
  return theme;
}

export function readChoice(): ThemeChoice {
  const stored = storage.read(STORAGE_KEY);
  return stored === 'light' || stored === 'dark' ? stored : 'system';
}

export function writeChoice(choice: ThemeChoice): void {
  if (choice === 'system') storage.clear(STORAGE_KEY);
  else storage.write(STORAGE_KEY, choice);
}

/**
 * The inline script that runs before paint. Kept in sync with `applyTheme`
 * by hand: it cannot import anything, because it has to run synchronously
 * before the app's JavaScript is fetched.
 */
export const themeScript = `(function(){try{var s=localStorage.getItem(${JSON.stringify(
  STORAGE_KEY,
)});var v=s==='light'||s==='dark'?s:'system';var t=v==='system'?(window.matchMedia&&window.matchMedia(${JSON.stringify(
  DARK_QUERY,
)}).matches?'dark':'light'):v;var r=document.documentElement;r.classList.remove('light','dark');r.classList.add(t);r.dataset.theme=v;var c=t==='dark'?'#0a0a0b':'#fafafa';var m=document.querySelectorAll('meta[name="theme-color"]');for(var i=0;i<m.length;i++){m[i].setAttribute('content',c);}}catch(e){}})();`;

/** The theme the user picked, plus the one actually in effect. */
export function useTheme(): {
  choice: ThemeChoice;
  theme: Theme;
  setChoice: (choice: ThemeChoice) => void;
  toggle: () => void;
} {
  const [choice, setChoiceState] = useState<ThemeChoice>('system');
  const [theme, setTheme] = useState<Theme>('light');

  useEffect(() => {
    const stored = readChoice();
    setChoiceState(stored);
    setTheme(applyTheme(stored));
  }, []);

  // Follow the OS while the choice is 'system', so the app tracks a
  // day/night switch on the machine without a reload.
  useEffect(() => {
    if (choice !== 'system' || typeof window === 'undefined' || !window.matchMedia) return;
    const query = window.matchMedia(DARK_QUERY);
    const onChange = () => setTheme(applyTheme('system'));
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, [choice]);

  const setChoice = useCallback((next: ThemeChoice) => {
    writeChoice(next);
    setChoiceState(next);
    setTheme(applyTheme(next));
  }, []);

  const toggle = useCallback(() => {
    // flip from what is actually on screen, not from storage: if storage is
    // blocked the choice is not persisted, and reading it back would leave
    // the switch stuck.
    setChoice(theme === 'dark' ? 'light' : 'dark');
  }, [setChoice, theme]);

  return { choice, theme, setChoice, toggle };
}
