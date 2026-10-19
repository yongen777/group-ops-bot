import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { api } from './api';
import type { GroupItem } from './types';

interface Me {
  username: string;
  must_change_password: boolean;
}

interface AppState {
  me: Me | null;
  groups: GroupItem[];
  groupsLoading: boolean;
  refreshGroups: () => Promise<GroupItem[]>;
  selectedGroupId: string;
  selectGroup: (groupId: string) => void;
  selectedGroup: GroupItem | undefined;
}

const AppContext = createContext<AppState | null>(null);

export const AppProvider: React.FC<{ me: Me; children: React.ReactNode }> = ({
  me,
  children,
}) => {
  const [groups, setGroups] = useState<GroupItem[]>([]);
  const [groupsLoading, setGroupsLoading] = useState(true);
  const [selectedGroupId, setSelectedGroupId] = useState('');

  const refreshGroups = useCallback(async () => {
    setGroupsLoading(true);
    try {
      const data = await api.get<{ ok: boolean; groups: GroupItem[] }>('/api/admin/groups');
      const list = data.groups || [];
      setGroups(list);
      return list;
    } finally {
      setGroupsLoading(false);
    }
  }, []);

  useEffect(() => {
    refreshGroups()
      .then((list) => {
        const fromUrl = new URLSearchParams(window.location.search).get('group_id') || '';
        const known = list.some((g) => g.group_id === fromUrl);
        const active = list.filter((g) => g.status === 'active');
        const initial = known ? fromUrl : (active[0] || list[0])?.group_id || '';
        setSelectedGroupId((prev) => prev || initial);
      })
      .catch(() => setGroupsLoading(false));
  }, [refreshGroups]);

  const selectGroup = useCallback((groupId: string) => {
    setSelectedGroupId(groupId);
    const url = new URL(window.location.href);
    url.searchParams.set('group_id', groupId);
    window.history.replaceState(null, '', url.toString());
  }, []);

  const selectedGroup = useMemo(
    () => groups.find((g) => g.group_id === selectedGroupId),
    [groups, selectedGroupId],
  );

  const value: AppState = {
    me,
    groups,
    groupsLoading,
    refreshGroups,
    selectedGroupId,
    selectGroup,
    selectedGroup,
  };

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
};

export function useApp(): AppState {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('AppProvider missing');
  return ctx;
}
