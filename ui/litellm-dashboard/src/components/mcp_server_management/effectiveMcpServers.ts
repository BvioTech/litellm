import { z } from "zod/v4";
import { MCPServer, MCPToolset } from "../mcp_tools/types";

// Mirrors the backend resolver's union (direct + access_group + tool_perm + toolset), so the
// editor shows exactly the servers this permission level entitles.
export type McpGrantSource =
  | { readonly kind: "direct" }
  | { readonly kind: "accessGroup"; readonly name: string }
  | { readonly kind: "toolset"; readonly name: string }
  | { readonly kind: "toolPermission" };

export interface EffectiveMcpServer {
  readonly server: MCPServer;
  // The mcp_tool_permissions key holding this server's allowlist. The backend accepts a server
  // id, name or alias interchangeably, so an API- or config-written entry may use any of them;
  // reading and writing the same key keeps an edit from leaving the original entry behind.
  readonly permissionKey: string;
  readonly source: McpGrantSource;
}

interface ResolveInput {
  readonly allServers: readonly MCPServer[];
  readonly selectedServers: readonly string[];
  readonly selectedAccessGroups: readonly string[];
  readonly selectedToolsets: readonly string[];
  readonly toolsets: readonly MCPToolset[];
  readonly toolPermissions: Readonly<Record<string, readonly string[]>>;
}

// Access groups come back as plain names, but older records carry `{ name }` objects.
const accessGroupRefSchema = z.union([z.string(), z.object({ name: z.string() })]);

const accessGroupNamesOf = (server: MCPServer): readonly string[] =>
  (server.mcp_access_groups ?? []).flatMap((group) => {
    const parsed = accessGroupRefSchema.safeParse(group);
    if (!parsed.success) return [];
    return [typeof parsed.data === "string" ? parsed.data : parsed.data.name];
  });

export const mcpServerMatchesIdentifier = (server: MCPServer, identifier: string): boolean =>
  server.server_id === identifier || server.server_name === identifier || server.alias === identifier;

export const mcpToolPermissionKeyFor = (
  server: MCPServer,
  toolPermissions: Readonly<Record<string, readonly string[]>>,
): string =>
  [server.server_id, server.server_name, server.alias].find(
    (identifier): identifier is string => typeof identifier === "string" && Object.hasOwn(toolPermissions, identifier),
  ) ?? server.server_id;

export const resolveEffectiveMcpServers = ({
  allServers,
  selectedServers,
  selectedAccessGroups,
  selectedToolsets,
  toolsets,
  toolPermissions,
}: ResolveInput): readonly EffectiveMcpServer[] => {
  const entry = (server: MCPServer, source: McpGrantSource): EffectiveMcpServer => ({
    server,
    permissionKey: mcpToolPermissionKeyFor(server, toolPermissions),
    source,
  });

  const direct = selectedServers.flatMap((identifier) =>
    allServers
      .filter((server) => mcpServerMatchesIdentifier(server, identifier))
      .map((server) => entry(server, { kind: "direct" })),
  );

  const viaAccessGroups = selectedAccessGroups.flatMap((group) =>
    allServers
      .filter((server) => accessGroupNamesOf(server).includes(group))
      .map((server) => entry(server, { kind: "accessGroup", name: group })),
  );

  const viaToolsets = selectedToolsets.flatMap((toolsetId) => {
    const toolset = toolsets.find((candidate) => candidate.toolset_id === toolsetId);
    if (!toolset) return [];
    const toolsetServerIds = new Set(toolset.tools.map((tool) => tool.server_id));
    return allServers
      .filter((server) => toolsetServerIds.has(server.server_id))
      .map((server) => entry(server, { kind: "toolset", name: toolset.toolset_name }));
  });

  // A server named only under mcp_tool_permissions is entitled on purpose, so it belongs in the
  // editor: without it, an entry left over from a removed access group is invisible and unclearable.
  const viaToolPermissions = Object.keys(toolPermissions).flatMap((key) =>
    allServers
      .filter((server) => mcpServerMatchesIdentifier(server, key))
      .map((server) => entry(server, { kind: "toolPermission" })),
  );

  const candidates = [...direct, ...viaAccessGroups, ...viaToolsets, ...viaToolPermissions];
  return candidates.filter(
    (candidate, index) =>
      candidates.findIndex((other) => other.server.server_id === candidate.server.server_id) === index,
  );
};
