import { createContext } from "react";
import type { Locale, Translations } from "./types";
import { en } from "./en";

export interface I18nContextValue {
  locale: Locale;
  setLocale: (l: Locale) => void;
  t: Translations;
}

export const I18nContext = createContext<I18nContextValue>({
  locale: "en",
  setLocale: () => {},
  t: en,
});
