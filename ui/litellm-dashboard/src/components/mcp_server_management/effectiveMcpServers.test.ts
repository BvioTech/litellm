import { describe, it, expect } from "vitest";
import { MCPServer, MCPToolset } from "../mcp_tools/types";
import { mcpServerMatchesIdentifier, mcpToolPermissionKeyFor, resolveEffectiveMcpServers } from "./effectiveMcpServers";

const server = (overrides: Partial<MCPServer> & { server_id: string }): MCPServer =>
  ({
    server_name: null,
    alias: null,
    created_at: "2026-01-01",
    created_by: "admin",
    updated_at: "2026-01-01",
    updated_by: "admin",
    ...overrides,
  }) as MCPServer;

const toolset = (overrides: Partial<MCPToolset> & { toolset_id: string }): MCPToolset =>
  ({ toolset_name: overrides.toolset_id, tools: [], ...overrides }) as MCPToolset;

const emptyInput = {
  allServers: [] as readonly MCPServer[],
  selectedServers: [] as readonly string[],
  selectedAccessGroups: [] as readonly string[],
  selectedToolsets: [] as readonly string[],
  toolsets: [] as readonly MCPToolset[],
  toolPermissions: {} as Readonly<Record<string, readonly string[]>>,
};

describe("mcpServerMatchesIdentifier", () => {
  it("matches on id, server name and alias alike", () => {
    const target = server({ server_id: "uuid-1", server_name: "github_mcp", alias: "GitHub" });

    expect(mcpServerMatchesIdentifier(target, "uuid-1")).toBe(true);
    expect(mcpServerMatchesIdentifier(target, "github_mcp")).toBe(true);
    expect(mcpServerMatchesIdentifier(target, "GitHub")).toBe(true);
    expect(mcpServerMatchesIdentifier(target, "other")).toBe(false);
  });
});

describe("mcpToolPermissionKeyFor", () => {
  const target = server({ server_id: "uuid-1", server_name: "github_mcp", alias: "GitHub" });

  it("returns the existing name key so an edit does not fork into a second entry", () => {
    expect(mcpToolPermissionKeyFor(target, { github_mcp: ["list_issues"] })).toBe("github_mcp");
  });

  it("prefers the id key when both an id and a name key exist", () => {
    expect(mcpToolPermissionKeyFor(target, { github_mcp: ["list_issues"], "uuid-1": ["list_prs"] })).toBe("uuid-1");
  });

  it("falls back to the server id when no entry exists yet", () => {
    expect(mcpToolPermissionKeyFor(target, {})).toBe("uuid-1");
  });
});

describe("resolveEffectiveMcpServers", () => {
  const direct = server({ server_id: "srv-direct", server_name: "Direct" });
  const grouped = server({ server_id: "srv-group", server_name: "Grouped", mcp_access_groups: ["prod"] });
  const inToolset = server({ server_id: "srv-toolset", server_name: "Toolsetted" });

  it("resolves a selected access group to its member servers", () => {
    const resolved = resolveEffectiveMcpServers({
      ...emptyInput,
      allServers: [direct, grouped],
      selectedAccessGroups: ["prod"],
    });

    expect(resolved).toEqual([
      { server: grouped, permissionKey: "srv-group", source: { kind: "accessGroup", name: "prod" } },
    ]);
  });

  it("resolves access groups stored as objects rather than plain names", () => {
    const objectGrouped = { ...grouped, mcp_access_groups: [{ name: "prod" }] } as unknown as MCPServer;

    const resolved = resolveEffectiveMcpServers({
      ...emptyInput,
      allServers: [objectGrouped],
      selectedAccessGroups: ["prod"],
    });

    expect(resolved.map((entry) => entry.server.server_id)).toEqual(["srv-group"]);
  });

  it("resolves a selected toolset to the servers its tools live on", () => {
    const resolved = resolveEffectiveMcpServers({
      ...emptyInput,
      allServers: [direct, inToolset],
      selectedToolsets: ["ts-1"],
      toolsets: [
        toolset({
          toolset_id: "ts-1",
          toolset_name: "Support",
          tools: [{ server_id: "srv-toolset", tool_name: "list_issues" }],
        }),
      ],
    });

    expect(resolved).toEqual([
      { server: inToolset, permissionKey: "srv-toolset", source: { kind: "toolset", name: "Support" } },
    ]);
  });

  it("yields nothing for a toolset that is not in the loaded list", () => {
    const resolved = resolveEffectiveMcpServers({
      ...emptyInput,
      allServers: [inToolset],
      selectedToolsets: ["ts-missing"],
      toolsets: [],
    });

    expect(resolved).toEqual([]);
  });

  it("includes a server that only a tool-permission entry names", () => {
    const resolved = resolveEffectiveMcpServers({
      ...emptyInput,
      allServers: [grouped],
      toolPermissions: { "srv-group": ["list_issues"] },
    });

    expect(resolved).toEqual([{ server: grouped, permissionKey: "srv-group", source: { kind: "toolPermission" } }]);
  });

  it("reports a server once, attributing it to the strongest grant", () => {
    const resolved = resolveEffectiveMcpServers({
      ...emptyInput,
      allServers: [grouped],
      selectedServers: ["srv-group"],
      selectedAccessGroups: ["prod"],
      toolPermissions: { "srv-group": ["list_issues"] },
    });

    expect(resolved).toEqual([{ server: grouped, permissionKey: "srv-group", source: { kind: "direct" } }]);
  });

  it("resolves a server selected by name", () => {
    const named = server({ server_id: "uuid-1", server_name: "github_mcp", alias: "GitHub" });

    const resolved = resolveEffectiveMcpServers({
      ...emptyInput,
      allServers: [named],
      selectedServers: ["github_mcp"],
      toolPermissions: { github_mcp: ["list_issues"] },
    });

    expect(resolved).toEqual([{ server: named, permissionKey: "github_mcp", source: { kind: "direct" } }]);
  });

  it("resolves every server sharing a duplicated name, as the backend does", () => {
    const first = server({ server_id: "uuid-1", server_name: "shared" });
    const second = server({ server_id: "uuid-2", server_name: "shared" });

    const resolved = resolveEffectiveMcpServers({
      ...emptyInput,
      allServers: [first, second],
      selectedServers: ["shared"],
    });

    expect(resolved.map((entry) => entry.server.server_id)).toEqual(["uuid-1", "uuid-2"]);
  });
});
