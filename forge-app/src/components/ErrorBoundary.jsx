import { Component } from 'react';
import { Text, Box, Code, Heading, xcss } from '@forge/react';

const errorStyles = xcss({
  padding: 'space.200',
  backgroundColor: 'color.background.danger.subtle',
  borderRadius: 'border.radius.100',
});

const codeStyles = xcss({
  marginTop: 'space.100',
  padding: 'space.100',
  backgroundColor: 'color.background.neutral',
  borderRadius: 'border.radius.100',
});

export class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, errorInfo) {
    console.error('ErrorBoundary caught an error:', error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return (
        <Box xcss={errorStyles}>
          <Heading as="h3">Something went wrong</Heading>
          <Text>We&apos;re sorry, but the application encountered an unexpected error.</Text>
          <Box xcss={codeStyles}>
            <Code>{this.state.error?.message || 'Unknown error'}</Code>
          </Box>
        </Box>
      );
    }
    return this.props.children;
  }
}
