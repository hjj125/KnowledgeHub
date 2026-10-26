import { ref } from "vue";

const isDark = ref(document.documentElement.classList.contains("dark"));

export function useTheme() {
  function toggle(): void {
    isDark.value = !isDark.value;
    document.documentElement.classList.toggle("dark", isDark.value);
    localStorage.setItem("kh_theme", isDark.value ? "dark" : "light");
  }
  return { isDark, toggle };
}
