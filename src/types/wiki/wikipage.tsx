// Wiki Interfaces
export interface CitationInfo {
  status: 'verified' | 'broken';
  filePath: string;
  startLine?: number;
  endLine?: number;
  snippet?: string;
  reason?: string;
}

export interface WikiPage {
  id: string;
  title: string;
  content: string;
  filePaths: string[];
  importance: 'high' | 'medium' | 'low';
  relatedPages: string[];
  citations?: Record<string, CitationInfo>;
  // New fields for hierarchy
  parentId?: string;
  isSection?: boolean;
  children?: string[]; // IDs of child pages
}