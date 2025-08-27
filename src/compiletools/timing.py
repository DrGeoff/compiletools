import time
import sys
from contextlib import contextmanager
from collections import defaultdict, OrderedDict


class Timer:
    """Timer class for tracking elapsed time of operations in compiletools.
    
    Supports nested timing contexts and hierarchical reporting based on verbose levels.
    """
    
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.timings = OrderedDict()  # Operation name -> elapsed time
        self.nested_timings = defaultdict(list)  # Parent -> list of child timings
        self.start_times = {}  # Operation name -> start time
        self.operation_stack = []  # Stack for nested operations
        self.operation_filenames = {}  # Operation name -> filename for operations with files
        
    def start(self, operation_name):
        """Start timing an operation."""
        if not self.enabled:
            return
            
        current_time = time.perf_counter()
        self.start_times[operation_name] = current_time
        
        # Track nesting
        if self.operation_stack:
            parent = self.operation_stack[-1]
            self.nested_timings[parent].append(operation_name)
        
        self.operation_stack.append(operation_name)
    
    def register_file_operation(self, operation_name, filename):
        """Register that an operation name contains a specific filename."""
        if self.enabled:
            self.operation_filenames[operation_name] = filename
    
    def stop(self, operation_name):
        """Stop timing an operation and record elapsed time."""
        if not self.enabled:
            return 0.0
            
        current_time = time.perf_counter()
        
        if operation_name not in self.start_times:
            return 0.0
            
        elapsed = current_time - self.start_times[operation_name]
        self.timings[operation_name] = elapsed
        
        # Remove from stack
        if self.operation_stack and self.operation_stack[-1] == operation_name:
            self.operation_stack.pop()
        
        del self.start_times[operation_name]
        return elapsed
    
    @contextmanager
    def time_operation(self, operation_name):
        """Context manager for timing operations."""
        self.start(operation_name)
        try:
            yield
        finally:
            self.stop(operation_name)
    
    @contextmanager
    def time_file_operation(self, operation_prefix, filename):
        """Context manager for timing operations that involve a specific file."""
        import compiletools.wrappedos
        basename = compiletools.wrappedos.basename(filename)
        operation_name = f"{operation_prefix}_{basename}"
        
        self.register_file_operation(operation_name, basename)
        self.start(operation_name)
        try:
            yield
        finally:
            self.stop(operation_name)
    
    def get_elapsed(self, operation_name):
        """Get elapsed time for an operation."""
        return self.timings.get(operation_name, 0.0)
    
    def format_time(self, seconds):
        """Format time in microseconds for precision."""
        microseconds = seconds * 1_000_000
        if microseconds < 1000:
            return f"{microseconds:.0f}µs"
        elif microseconds < 1_000_000:
            return f"{microseconds / 1000:.1f}ms"
        elif seconds < 60.0:
            return f"{seconds:.1f}s"
        else:
            minutes = int(seconds // 60)
            secs = seconds % 60
            return f"{minutes}m{secs:.1f}s"
    
    def report(self, verbose_level, file=None):
        """Generate timing report based on verbose level."""
        if not self.enabled or not self.timings:
            return
        
        if file is None:
            file = sys.stderr
        
        # Calculate total time from top-level operations only to avoid double-counting
        all_nested = set()
        for children in self.nested_timings.values():
            all_nested.update(children)
        
        top_level_ops = [op for op in self.timings if op not in all_nested]
        total_time = sum(self.timings[op] for op in top_level_ops) if top_level_ops else sum(self.timings.values())
        
        if verbose_level >= 0:
            print(f"Total build time: {self.format_time(total_time)}", file=file)
        
        if verbose_level == 1:
            print("\nOperations by category:", file=file)
            self._report_operation_groups(file=file, verbose_level=verbose_level)
        
        if verbose_level >= 2:
            print("\nDetailed timing breakdown:", file=file)
            # Use the same hierarchy as "Operations by category" but without aggregation
            hierarchy = self._build_operation_hierarchy()
            if hierarchy:
                # Show full detail without aggregation at verbose level 2+
                max_depth = None  # No depth limit for detailed view
                self._report_hierarchy_recursive(hierarchy, file=file, indent=0, 
                                               verbose_level=verbose_level + 10)  # High level to prevent aggregation
    
    def get_summary(self):
        """Get a summary dictionary of timing information."""
        if not self.enabled:
            return {}
        
        return {
            'total_time': sum(self.timings.values()),
            'operation_count': len(self.timings),
            'slowest_operation': max(self.timings.items(), key=lambda x: x[1]) if self.timings else None,
            'operations': dict(self.timings)
        }


    def _report_operation_groups(self, file=None, verbose_level=1):
        """Report aggregated statistics by operation category in hierarchical tree format."""
        if file is None:
            file = sys.stderr
        
        # Create hierarchical structure based on nested operations
        hierarchy = self._build_operation_hierarchy()
        if not hierarchy:
            return
        
        # Report the hierarchy with verbosity-based aggregation
        self._report_hierarchy_recursive(hierarchy, file=file, indent=0, verbose_level=verbose_level)

    def _build_operation_hierarchy(self):
        """Build a hierarchical structure based on actual timing nesting relationships."""
        if not self.timings:
            return {}
            
        # Find all operations that are not nested under others (top-level)
        all_nested = set()
        for parent, children in self.nested_timings.items():
            all_nested.update(children)
        
        top_level_ops = [op for op in self.timings.keys() if op not in all_nested]
        
        # Build hierarchy recursively from top-level operations with cycle detection
        hierarchy = {}
        visited = set()
        for op in top_level_ops:
            hierarchy[op] = self._build_operation_node(op, visited.copy())
            
        return hierarchy
    
    def _build_operation_node(self, operation_name, visited=None):
        """Build a single node in the hierarchy with its children."""
        if visited is None:
            visited = set()
        
        # Prevent infinite recursion by checking if we've already visited this node
        if operation_name in visited:
            return {
                'time': self.timings.get(operation_name, 0.0),
                'children': {}  # Don't recurse into already visited nodes
            }
        
        visited.add(operation_name)
        
        node = {
            'time': self.timings.get(operation_name, 0.0),
            'children': {}
        }
        
        # Add direct children
        if operation_name in self.nested_timings:
            for child_name in self.nested_timings[operation_name]:
                if child_name in self.timings:
                    node['children'][child_name] = self._build_operation_node(child_name, visited.copy())
        
        return node


    def _report_hierarchy_recursive(self, hierarchy, file=None, indent=0, threshold_ms=1.0, verbose_level=1, already_aggregated=False):
        """Recursively report the timing hierarchy based on actual execution nesting."""
        if file is None:
            file = sys.stderr
        
        # At lower verbosity levels, aggregate per-file operations for cleaner overview
        # Only do aggregation at the top level and skip for large hierarchies to avoid slowness
        if verbose_level == 1 and not already_aggregated:
            total_operations = self._count_total_operations(hierarchy)
            if total_operations < 1000:  # Only aggregate for smaller hierarchies
                hierarchy = self._aggregate_per_file_operations(hierarchy)
            already_aggregated = True
        
        # Sort operations by time descending
        sorted_operations = sorted(hierarchy.items(), 
                                 key=lambda x: x[1]['time'], 
                                 reverse=True)
        
        for operation_name, node in sorted_operations:
            operation_time = node['time']
            
            # Skip very small operations at deeper levels to reduce noise
            if indent > 1 and operation_time * 1000 < threshold_ms:
                continue
            
            # Format the operation line
            indent_str = "  " * indent
            time_str = self.format_time(operation_time)
            
            # Check if this is an aggregated operation
            if isinstance(node.get('file_count'), int) and node['file_count'] > 1:
                print(f"{indent_str}{operation_name}: {time_str} ({node['file_count']} files)", file=file)
            else:
                print(f"{indent_str}{operation_name}: {time_str}", file=file)
            
            # Calculate children's total time for validation
            if node['children']:
                children_total = sum(child['time'] for child in node['children'].values())
                
                # Show warning if children time significantly exceeds parent time (timing overlap issue)
                if children_total > operation_time * 1.1:  # 10% tolerance for timing precision
                    print(f"{indent_str}  ⚠ Children time ({self.format_time(children_total)}) > parent time", file=file)
                
                # Recursively report children
                self._report_hierarchy_recursive(node['children'], file, indent + 1, threshold_ms, verbose_level, already_aggregated)

    def _aggregate_per_file_operations(self, hierarchy):
        """Aggregate per-file operations into summary operations using stored filename data."""
        aggregated = {}
        
        # Group operations by their base name using stored filename information
        operation_groups = defaultdict(list)
        
        for operation_name, node in hierarchy.items():
            # Use stored filename data for precise base name extraction
            base_name = self._extract_operation_base_name(operation_name)
            operation_groups[base_name].append((operation_name, node))
        
        # Create aggregated nodes
        for base_name, operations in operation_groups.items():
            if len(operations) == 1:
                # Single operation, no aggregation needed
                operation_name, node = operations[0]
                aggregated[operation_name] = node
            else:
                # Multiple operations with same base - aggregate them
                total_time = sum(node['time'] for _, node in operations)
                filenames = []
                
                # Collect filenames for display
                for operation_name, node in operations:
                    if operation_name in self.operation_filenames:
                        filenames.append(self.operation_filenames[operation_name])
                
                # For aggregated operations, don't show individual children
                # as that defeats the purpose of aggregation
                aggregated[base_name] = {
                    'time': total_time,
                    'children': {},  # No children for aggregated view
                    'file_count': len(operations),
                    'filenames': filenames  # Store the actual filenames for reference
                }
        
        return aggregated
    
    def _count_total_operations(self, hierarchy):
        """Count total number of operations in the hierarchy."""
        count = 0
        
        def count_recursive(node_hierarchy):
            nonlocal count
            count += len(node_hierarchy)
            for node in node_hierarchy.values():
                if node.get('children'):
                    count_recursive(node['children'])
        
        count_recursive(hierarchy)
        return count

    def _extract_operation_base_name(self, operation_name):
        """Extract the base operation name without filename."""
        # Use stored filename data for precise extraction
        if operation_name in self.operation_filenames:
            filename = self.operation_filenames[operation_name]
            if operation_name.endswith('_' + filename):
                return operation_name[:-len('_' + filename)]
        
        # Simple fallback for operations without stored filename data
        return operation_name

    def _get_filename_from_operation(self, operation_name):
        """Extract filename from an operation name."""
        # Return stored filename data if available
        if operation_name in self.operation_filenames:
            return self.operation_filenames[operation_name]
        
        # Simple fallback: return the operation name itself
        return operation_name


# Global timer instance for use throughout compiletools
_global_timer = Timer()


def get_timer():
    """Get the global timer instance."""
    return _global_timer


def initialize_timer(enabled=False):
    """Initialize the global timer."""
    global _global_timer
    _global_timer = Timer(enabled)


def time_operation(operation_name):
    """Context manager decorator for timing operations."""
    return _global_timer.time_operation(operation_name)


def time_file_operation(operation_prefix, filename):
    """Context manager for timing operations that involve a specific file."""
    return _global_timer.time_file_operation(operation_prefix, filename)


def start_timing(operation_name):
    """Start timing an operation using the global timer."""
    _global_timer.start(operation_name)


def stop_timing(operation_name):
    """Stop timing an operation using the global timer."""
    return _global_timer.stop(operation_name)


def report_timing(verbose_level, file=None):
    """Generate timing report using the global timer."""
    _global_timer.report(verbose_level, file)