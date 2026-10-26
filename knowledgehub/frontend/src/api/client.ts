import axios from "axios";

export const apiClient = axios.create({
  baseURL: "/api/v1",
  timeout: 30000,
});

const TOKEN_KEY = "kh_token";
const EMAIL_KEY = "kh_email";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(EMAIL_KEY);
}

export function savedEmail(): string {
  return localStorage.getItem(EMAIL_KEY) ?? "";
}

export function saveEmail(email: string): void {
  localStorage.setItem(EMAIL_KEY, email);
}

apiClient.interceptors.request.use((config) => {
  const token = getToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401 && !location.pathname.startsWith("/login")) {
      clearToken();
      location.href = "/login";
    }
    return Promise.reject(error);
  },
);
