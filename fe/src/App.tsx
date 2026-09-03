import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./components/AppShell";
import { ActivityScreen } from "./screens/ActivityScreen";
import { ApprovalsScreen } from "./screens/ApprovalsScreen";
import { ConnectScreen } from "./screens/ConnectScreen";
import { HomeScreen } from "./screens/HomeScreen";
import { InvoicesScreen } from "./screens/InvoicesScreen";
import { SignInScreen } from "./screens/SignInScreen";

export default function App() {
  return (
    <Routes>
      <Route path="/signin" element={<SignInScreen />} />
      <Route element={<AppShell />}>
        <Route index element={<HomeScreen />} />
        <Route path="approvals" element={<ApprovalsScreen />} />
        <Route path="invoices" element={<InvoicesScreen />} />
        <Route path="activity" element={<ActivityScreen />} />
        <Route path="connect" element={<ConnectScreen />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
