"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, ShieldAlert } from "lucide-react";
import { PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useAuth } from "@/components/AuthProvider";
import { apiFetch, problemDetail, type Principal } from "@/lib/api";

/**
 * Accounts — the user-management half of the admin role (#50).
 *
 * The nav entry is admin-only, but a reviewer can still type the URL (a static
 * export has no server to stop them), so the page checks the role itself — and
 * either way `/api/admin/users` answers 403. The compliance rules are readable by
 * every reviewer at /rules (#57); an admin editor for them would land here.
 */
export default function AdminPage() {
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");
  const [users, setUsers] = useState<Principal[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isAdmin) return;
    let active = true;
    apiFetch("/api/admin/users")
      .then(async (response) => {
        if (!active) return;
        if (!response.ok) {
          setError(await problemDetail(response, "Could not load accounts"));
          return;
        }
        setUsers((await response.json()) as Principal[]);
      })
      .catch(() => {
        if (active) setError("Could not reach the server.");
      });
    return () => {
      active = false;
    };
  }, [isAdmin]);

  if (!isAdmin) {
    return (
      <>
        <PageHeader title="Accounts" />
        <Card className="border-destructive/40 bg-destructive/10" role="alert">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ShieldAlert className="text-destructive size-4" aria-hidden="true" />
              Administrator access is required for this page
            </CardTitle>
            <CardDescription>
              Your account has the reviewer role, which covers screening and the approval gate but
              not account management.
            </CardDescription>
          </CardHeader>
        </Card>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Accounts"
        description="Reviewers can clear the human-in-the-loop approval gate; administrators can also manage accounts and compliance rules."
      />

      {error && (
        <Card className="border-destructive/40 bg-destructive/10 mb-4" role="alert">
          <CardContent className="flex items-start gap-2.5 text-sm">
            <AlertTriangle className="text-destructive mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>{error}</span>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent>
          {users === null && !error ? (
            <div className="space-y-2" aria-hidden="true">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Email</TableHead>
                  <TableHead>Role</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(users ?? []).map((user) => (
                  <TableRow key={user.email}>
                    <TableCell className="font-medium">{user.email}</TableCell>
                    <TableCell>
                      <Badge variant={user.role === "admin" ? "warn" : "secondary"}>
                        {user.role}
                      </Badge>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </>
  );
}
